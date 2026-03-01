package com.adbtoolkit.agent.security

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import android.util.Base64
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.security.*
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * Manages cryptographic pairing between agent instances.
 *
 * Security flow:
 * ┌────────────┐                           ┌────────────┐
 * │  Device A  │  1. POST /peer/pair-init  │  Device B  │
 * │ (initiator)│ ──── pubKeyA + label ───> │ (responder)│
 * │            │                           │            │
 * │            │  2. Shows confirm dialog  │            │
 * │            │     with 6-digit code     │ BIOMETRIC  │
 * │            │     + biometric gate      │  REQUIRED  │
 * │            │                           │            │
 * │            │  3. POST /peer/pair-accept│            │
 * │            │ <── pubKeyB + signature ──│            │
 * │            │                           │            │
 * │  ECDH ──> │  4. Both derive same      │ <── ECDH   │
 * │  shared   │     HMAC secret from      │   shared   │
 * │  secret   │     ECDH key agreement    │   secret   │
 * └────────────┘                           └────────────┘
 *
 * All subsequent P2P requests carry X-Peer-Signature (HMAC-SHA256).
 */
class PairingManager private constructor(private val context: Context) {

    companion object {
        private const val TAG = "PairingManager"
        private const val PREFS_NAME = "paired_devices"
        private const val KEY_DEVICES = "devices"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_KEYPAIR = "local_keypair"
        private const val EC_CURVE = "secp256r1"   // P-256 / prime256v1
        private const val HMAC_ALGO = "HmacSHA256"
        private const val CONFIRM_CODE_DIGITS = 6

        @Volatile
        private var instance: PairingManager? = null

        fun getInstance(context: Context): PairingManager {
            return instance ?: synchronized(this) {
                instance ?: PairingManager(context.applicationContext).also { instance = it }
            }
        }
    }

    /** This device's stable UUID (persisted). */
    val deviceId: String

    /** Local EC P-256 key pair for ECDH. */
    private val localKeyPair: KeyPair

    /** Base64 of the local public key (X.509 encoding). */
    val localPublicKeyB64: String

    /** All paired devices indexed by deviceId. */
    private val pairedDevices = ConcurrentHashMap<String, PairedDevice>()

    /**
     * Pending pairing requests awaiting biometric confirmation.
     * Key = challengeId (UUID), Value = pending info.
     */
    val pendingPairings = ConcurrentHashMap<String, PendingPairing>()

    private val prefs: SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    init {
        // ── Device ID ────────────────────────────────────────────────
        deviceId = prefs.getString(KEY_DEVICE_ID, null) ?: run {
            val id = UUID.randomUUID().toString()
            prefs.edit().putString(KEY_DEVICE_ID, id).apply()
            id
        }

        // ── Local key pair ───────────────────────────────────────────
        localKeyPair = loadOrGenerateKeyPair()
        localPublicKeyB64 = Base64.encodeToString(
            localKeyPair.public.encoded, Base64.NO_WRAP
        )

        // ── Load saved pairings ──────────────────────────────────────
        loadPairedDevices()

        Log.i(TAG, "PairingManager init — deviceId=$deviceId, paired=${pairedDevices.size}")
    }

    // ═══════════════════════════════════════════════════════════════════
    //  KEY GENERATION & ECDH
    // ═══════════════════════════════════════════════════════════════════

    private fun loadOrGenerateKeyPair(): KeyPair {
        val saved = prefs.getString(KEY_KEYPAIR, null)
        if (saved != null) {
            try {
                val parts = saved.split("|")
                val kf = KeyFactory.getInstance("EC")
                val pubBytes = Base64.decode(parts[0], Base64.NO_WRAP)
                val privBytes = Base64.decode(parts[1], Base64.NO_WRAP)
                val pub = kf.generatePublic(X509EncodedKeySpec(pubBytes))
                val priv = kf.generatePrivate(
                    java.security.spec.PKCS8EncodedKeySpec(privBytes)
                )
                return KeyPair(pub, priv)
            } catch (e: Exception) {
                Log.w(TAG, "Failed to load keypair, regenerating", e)
            }
        }
        // Generate new EC P-256 keypair
        val kpg = KeyPairGenerator.getInstance("EC")
        kpg.initialize(ECGenParameterSpec(EC_CURVE), SecureRandom())
        val kp = kpg.generateKeyPair()
        // Persist
        val pubB64 = Base64.encodeToString(kp.public.encoded, Base64.NO_WRAP)
        val privB64 = Base64.encodeToString(kp.private.encoded, Base64.NO_WRAP)
        prefs.edit().putString(KEY_KEYPAIR, "$pubB64|$privB64").apply()
        return kp
    }

    /**
     * Perform ECDH key agreement to derive a shared secret.
     * Returns the hex-encoded shared secret (32 bytes).
     */
    fun deriveSharedSecret(peerPublicKeyB64: String): String {
        val kf = KeyFactory.getInstance("EC")
        val peerPubBytes = Base64.decode(peerPublicKeyB64, Base64.NO_WRAP)
        val peerPub = kf.generatePublic(X509EncodedKeySpec(peerPubBytes))

        val ka = KeyAgreement.getInstance("ECDH")
        ka.init(localKeyPair.private)
        ka.doPhase(peerPub, true)
        val secret = ka.generateSecret()

        // HKDF-like: SHA-256 the raw ECDH output for a clean 256-bit key
        val digest = MessageDigest.getInstance("SHA-256")
        val derived = digest.digest(secret)
        return derived.joinToString("") { "%02x".format(it) }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  CONFIRMATION CODE
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Generate a deterministic 6-digit confirmation code from both public keys.
     * Both devices compute the same code, so the user can visually verify.
     */
    fun generateConfirmCode(pubKeyA: String, pubKeyB: String): String {
        val combined = if (pubKeyA < pubKeyB) "$pubKeyA|$pubKeyB" else "$pubKeyB|$pubKeyA"
        val digest = MessageDigest.getInstance("SHA-256")
        val hash = digest.digest(combined.toByteArray(Charsets.UTF_8))
        // Take first 4 bytes as unsigned int, mod 10^6
        val num = ((hash[0].toLong() and 0xFF) shl 24) or
                  ((hash[1].toLong() and 0xFF) shl 16) or
                  ((hash[2].toLong() and 0xFF) shl 8) or
                  (hash[3].toLong() and 0xFF)
        val code = (num % 1_000_000).let { if (it < 0) -it else it }
        return code.toString().padStart(CONFIRM_CODE_DIGITS, '0')
    }

    // ═══════════════════════════════════════════════════════════════════
    //  PAIRING FLOW
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Called on the RESPONDER when it receives a pair-init request.
     * Creates a pending pairing that must be approved via biometric.
     *
     * @return challengeId to track this pending approval
     */
    fun createPendingPairing(
        peerDeviceId: String,
        peerLabel: String,
        peerPublicKeyB64: String,
        peerAddress: String,
    ): PendingPairing {
        val challengeId = UUID.randomUUID().toString()
        val confirmCode = generateConfirmCode(localPublicKeyB64, peerPublicKeyB64)

        val pending = PendingPairing(
            challengeId = challengeId,
            peerDeviceId = peerDeviceId,
            peerLabel = peerLabel,
            peerPublicKey = peerPublicKeyB64,
            peerAddress = peerAddress,
            confirmCode = confirmCode,
            createdAt = System.currentTimeMillis(),
        )
        pendingPairings[challengeId] = pending
        Log.i(TAG, "Pending pairing created: $challengeId for $peerLabel (code=$confirmCode)")
        return pending
    }

    /**
     * Called AFTER biometric/device-lock confirmation succeeds.
     * Completes the ECDH exchange and stores the pairing.
     *
     * @return the new PairedDevice, or null if challengeId is invalid/expired
     */
    fun approvePairing(challengeId: String): PairedDevice? {
        val pending = pendingPairings.remove(challengeId)
            ?: return null

        // Check expiry — pairings expire after 5 minutes
        if (System.currentTimeMillis() - pending.createdAt > 5 * 60 * 1000) {
            Log.w(TAG, "Pairing $challengeId expired")
            return null
        }

        val sharedSecret = deriveSharedSecret(pending.peerPublicKey)

        val device = PairedDevice(
            deviceId = pending.peerDeviceId,
            label = pending.peerLabel,
            sharedSecret = sharedSecret,
            publicKey = pending.peerPublicKey,
            lastAddress = pending.peerAddress,
            pairedAt = System.currentTimeMillis(),
            lastSeen = System.currentTimeMillis(),
            trusted = true,
        )
        pairedDevices[device.deviceId] = device
        savePairedDevices()

        Log.i(TAG, "Pairing approved: ${device.label} (${device.deviceId})")
        return device
    }

    /** Reject / cancel a pending pairing. */
    fun rejectPairing(challengeId: String) {
        val removed = pendingPairings.remove(challengeId)
        if (removed != null) {
            Log.i(TAG, "Pairing rejected: ${removed.peerLabel}")
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  HMAC SIGNATURE VALIDATION
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Sign a message (typically "METHOD|URI|timestamp") with a peer's shared secret.
     */
    fun sign(peerDeviceId: String, message: String): String? {
        val device = pairedDevices[peerDeviceId] ?: return null
        return hmacSign(device.sharedSecret, message)
    }

    /**
     * Verify a peer's HMAC signature.
     *
     * @param peerDeviceId  the claiming device
     * @param message       the canonical message that was signed
     * @param signature     the hex HMAC provided by the peer
     * @param maxAgeSec     maximum age of the timestamp in the message (replay protection)
     */
    fun verify(peerDeviceId: String, message: String, signature: String): Boolean {
        val device = pairedDevices[peerDeviceId]
        if (device == null || !device.trusted) {
            Log.w(TAG, "verify: unknown or untrusted device $peerDeviceId")
            return false
        }
        val expected = hmacSign(device.sharedSecret, message)
        val valid = MessageDigest.isEqual(
            expected.toByteArray(Charsets.UTF_8),
            signature.toByteArray(Charsets.UTF_8)
        )
        if (valid) {
            // Update last seen
            pairedDevices[peerDeviceId] = device.copy(lastSeen = System.currentTimeMillis())
        } else {
            Log.w(TAG, "HMAC verification FAILED for $peerDeviceId")
        }
        return valid
    }

    /**
     * Validate P2P request headers. Extracts:
     *   X-Peer-Id       → device ID of the caller
     *   X-Peer-Signature → HMAC of "METHOD|URI|X-Peer-Timestamp"
     *   X-Peer-Timestamp → epoch millis (replay window = 5 min)
     */
    fun validatePeerRequest(
        method: String,
        uri: String,
        headers: Map<String, String>,
    ): PeerValidation {
        val peerId = headers["x-peer-id"]
            ?: return PeerValidation(false, "Missing X-Peer-Id header")
        val sig = headers["x-peer-signature"]
            ?: return PeerValidation(false, "Missing X-Peer-Signature header")
        val tsStr = headers["x-peer-timestamp"]
            ?: return PeerValidation(false, "Missing X-Peer-Timestamp header")

        val ts = tsStr.toLongOrNull()
            ?: return PeerValidation(false, "Invalid X-Peer-Timestamp")

        // Replay protection: 5-minute window
        val age = Math.abs(System.currentTimeMillis() - ts)
        if (age > 5 * 60 * 1000) {
            return PeerValidation(false, "Request expired (age=${age}ms)")
        }

        val message = "$method|$uri|$tsStr"
        return if (verify(peerId, message, sig)) {
            PeerValidation(true, "OK", peerId)
        } else {
            PeerValidation(false, "HMAC verification failed for $peerId")
        }
    }

    private fun hmacSign(secretHex: String, message: String): String {
        val keyBytes = secretHex.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
        val mac = Mac.getInstance(HMAC_ALGO)
        mac.init(SecretKeySpec(keyBytes, HMAC_ALGO))
        val sig = mac.doFinal(message.toByteArray(Charsets.UTF_8))
        return sig.joinToString("") { "%02x".format(it) }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  DEVICE MANAGEMENT
    // ═══════════════════════════════════════════════════════════════════

    fun getPairedDevices(): List<PairedDevice> = pairedDevices.values.toList()

    fun getPairedDevice(deviceId: String): PairedDevice? = pairedDevices[deviceId]

    fun isPaired(deviceId: String): Boolean =
        pairedDevices[deviceId]?.trusted == true

    fun revokePairing(deviceId: String): Boolean {
        val removed = pairedDevices.remove(deviceId)
        if (removed != null) {
            savePairedDevices()
            Log.i(TAG, "Pairing revoked: ${removed.label} ($deviceId)")
        }
        return removed != null
    }

    fun revokeAll() {
        pairedDevices.clear()
        savePairedDevices()
        Log.i(TAG, "All pairings revoked")
    }

    fun updatePeerAddress(deviceId: String, address: String) {
        pairedDevices[deviceId]?.let {
            pairedDevices[deviceId] = it.copy(
                lastAddress = address,
                lastSeen = System.currentTimeMillis()
            )
        }
    }

    fun getDeviceLabel(): String {
        return "${Build.MANUFACTURER} ${Build.MODEL} — Android ${Build.VERSION.RELEASE}"
    }

    // ═══════════════════════════════════════════════════════════════════
    //  PERSISTENCE
    // ═══════════════════════════════════════════════════════════════════

    private fun savePairedDevices() {
        val arr = JSONArray()
        pairedDevices.values.forEach { arr.put(it.toJson()) }
        prefs.edit().putString(KEY_DEVICES, arr.toString()).apply()
    }

    private fun loadPairedDevices() {
        val raw = prefs.getString(KEY_DEVICES, null) ?: return
        try {
            val arr = JSONArray(raw)
            for (i in 0 until arr.length()) {
                val d = PairedDevice.fromJson(arr.getJSONObject(i))
                pairedDevices[d.deviceId] = d
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load paired devices", e)
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════
//  Supporting data classes
// ═══════════════════════════════════════════════════════════════════════

/** Pending pairing awaiting biometric confirmation on the responder. */
data class PendingPairing(
    val challengeId: String,
    val peerDeviceId: String,
    val peerLabel: String,
    val peerPublicKey: String,
    val peerAddress: String,
    val confirmCode: String,
    val createdAt: Long,
) {
    /** Whether this pairing request has expired (5 min). */
    val isExpired: Boolean
        get() = System.currentTimeMillis() - createdAt > 5 * 60 * 1000

    fun toJson(): JSONObject = JSONObject().apply {
        put("challenge_id", challengeId)
        put("peer_device_id", peerDeviceId)
        put("peer_label", peerLabel)
        put("confirm_code", confirmCode)
        put("created_at", createdAt)
        put("expired", isExpired)
    }
}

/** Result of P2P request validation. */
data class PeerValidation(
    val valid: Boolean,
    val reason: String,
    val peerId: String? = null,
)

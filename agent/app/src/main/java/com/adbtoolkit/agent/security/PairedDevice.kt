package com.adbtoolkit.agent.security

import org.json.JSONObject

/**
 * Represents a peer device that has been cryptographically paired
 * and biometrically approved by the user.
 */
data class PairedDevice(
    /** Unique device ID (UUID generated at first boot of the agent). */
    val deviceId: String,
    /** Human-readable label, e.g. "Xiaomi – Android 15". */
    val label: String,
    /** HMAC-SHA256 shared secret derived from ECDH key exchange (hex). */
    val sharedSecret: String,
    /** Public key of the peer (Base64-encoded X.509/EC P-256). */
    val publicKey: String,
    /** IP:port at last seen. */
    val lastAddress: String = "",
    /** Epoch millis when pairing was approved. */
    val pairedAt: Long = System.currentTimeMillis(),
    /** Epoch millis of last successful communication. */
    val lastSeen: Long = System.currentTimeMillis(),
    /** Whether this pairing is currently active/trusted. */
    val trusted: Boolean = true,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("device_id", deviceId)
        put("label", label)
        put("shared_secret", sharedSecret)
        put("public_key", publicKey)
        put("last_address", lastAddress)
        put("paired_at", pairedAt)
        put("last_seen", lastSeen)
        put("trusted", trusted)
    }

    /** Public-safe JSON (no secret). */
    fun toPublicJson(): JSONObject = JSONObject().apply {
        put("device_id", deviceId)
        put("label", label)
        put("last_address", lastAddress)
        put("paired_at", pairedAt)
        put("last_seen", lastSeen)
        put("trusted", trusted)
    }

    companion object {
        fun fromJson(json: JSONObject): PairedDevice = PairedDevice(
            deviceId     = json.getString("device_id"),
            label        = json.getString("label"),
            sharedSecret = json.getString("shared_secret"),
            publicKey    = json.getString("public_key"),
            lastAddress  = json.optString("last_address", ""),
            pairedAt     = json.optLong("paired_at", 0),
            lastSeen     = json.optLong("last_seen", 0),
            trusted      = json.optBoolean("trusted", true),
        )
    }
}

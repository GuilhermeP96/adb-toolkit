package com.adbtoolkit.agent.api

import android.content.ContentResolver
import android.content.Context
import android.database.Cursor
import android.net.Uri
import android.provider.ContactsContract
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONArray
import org.json.JSONObject

/**
 * Contacts API — direct ContentResolver access (no `adb backup` needed).
 *
 * Endpoints:
 *   GET  /api/contacts/list                → all contacts as JSON
 *   GET  /api/contacts/export-vcf          → VCF text stream
 *   GET  /api/contacts/count
 *   POST /api/contacts/import-vcf          → body = VCF text
 */
class ContactsApi(private val context: Context) {

    private val cr: ContentResolver get() = context.contentResolver

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""

        return when (action) {
            "list"       -> listContacts()
            "export-vcf" -> exportVcf()
            "count"      -> countContacts()
            "import-vcf" -> importVcf(session)
            else -> jsonError("Unknown contacts action: $action")
        }
    }

    // ── list ─────────────────────────────────────────────────────────────
    private fun listContacts(): Response {
        val contacts = JSONArray()

        // Query contacts
        val contactCursor = cr.query(
            ContactsContract.Contacts.CONTENT_URI,
            arrayOf(
                ContactsContract.Contacts._ID,
                ContactsContract.Contacts.DISPLAY_NAME_PRIMARY,
                ContactsContract.Contacts.HAS_PHONE_NUMBER,
                ContactsContract.Contacts.STARRED,
                ContactsContract.Contacts.PHOTO_URI,
            ),
            null, null,
            ContactsContract.Contacts.DISPLAY_NAME_PRIMARY + " ASC"
        )

        contactCursor?.use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getString(0) ?: continue
                val name = cursor.getString(1) ?: ""
                val hasPhone = (cursor.getInt(2) ?: 0) > 0
                val starred = (cursor.getInt(3) ?: 0) > 0

                val contact = JSONObject().apply {
                    put("id", id)
                    put("name", name)
                    put("starred", starred)
                }

                // Get phone numbers
                if (hasPhone) {
                    val phones = JSONArray()
                    val phoneCursor = cr.query(
                        ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                        arrayOf(
                            ContactsContract.CommonDataKinds.Phone.NUMBER,
                            ContactsContract.CommonDataKinds.Phone.TYPE,
                        ),
                        "${ContactsContract.CommonDataKinds.Phone.CONTACT_ID} = ?",
                        arrayOf(id), null
                    )
                    phoneCursor?.use { pc ->
                        while (pc.moveToNext()) {
                            phones.put(JSONObject().apply {
                                put("number", pc.getString(0) ?: "")
                                put("type", pc.getInt(1))
                            })
                        }
                    }
                    contact.put("phones", phones)
                }

                // Get emails
                val emails = JSONArray()
                val emailCursor = cr.query(
                    ContactsContract.CommonDataKinds.Email.CONTENT_URI,
                    arrayOf(
                        ContactsContract.CommonDataKinds.Email.ADDRESS,
                        ContactsContract.CommonDataKinds.Email.TYPE,
                    ),
                    "${ContactsContract.CommonDataKinds.Email.CONTACT_ID} = ?",
                    arrayOf(id), null
                )
                emailCursor?.use { ec ->
                    while (ec.moveToNext()) {
                        emails.put(JSONObject().apply {
                            put("email", ec.getString(0) ?: "")
                            put("type", ec.getInt(1))
                        })
                    }
                }
                if (emails.length() > 0) contact.put("emails", emails)

                contacts.put(contact)
            }
        }

        return jsonOk(JSONObject().apply {
            put("count", contacts.length())
            put("contacts", contacts)
        })
    }

    // ── export VCF ───────────────────────────────────────────────────────
    private fun exportVcf(): Response {
        val vcfBuilder = StringBuilder()

        val cursor = cr.query(
            ContactsContract.Contacts.CONTENT_URI,
            arrayOf(
                ContactsContract.Contacts._ID,
                ContactsContract.Contacts.DISPLAY_NAME_PRIMARY,
                ContactsContract.Contacts.HAS_PHONE_NUMBER,
            ),
            null, null, null
        )

        cursor?.use { c ->
            while (c.moveToNext()) {
                val id = c.getString(0) ?: continue
                val name = c.getString(1) ?: "Unknown"
                val hasPhone = (c.getInt(2) ?: 0) > 0

                vcfBuilder.appendLine("BEGIN:VCARD")
                vcfBuilder.appendLine("VERSION:3.0")
                vcfBuilder.appendLine("FN:$name")

                // Name parts (best effort)
                val nameParts = name.split(" ", limit = 2)
                val lastName = if (nameParts.size > 1) nameParts[1] else ""
                val firstName = nameParts[0]
                vcfBuilder.appendLine("N:$lastName;$firstName;;;")

                // Phone numbers
                if (hasPhone) {
                    val phoneCursor = cr.query(
                        ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                        arrayOf(ContactsContract.CommonDataKinds.Phone.NUMBER),
                        "${ContactsContract.CommonDataKinds.Phone.CONTACT_ID} = ?",
                        arrayOf(id), null
                    )
                    phoneCursor?.use { pc ->
                        while (pc.moveToNext()) {
                            val number = pc.getString(0) ?: continue
                            vcfBuilder.appendLine("TEL:$number")
                        }
                    }
                }

                // Emails
                val emailCursor = cr.query(
                    ContactsContract.CommonDataKinds.Email.CONTENT_URI,
                    arrayOf(ContactsContract.CommonDataKinds.Email.ADDRESS),
                    "${ContactsContract.CommonDataKinds.Email.CONTACT_ID} = ?",
                    arrayOf(id), null
                )
                emailCursor?.use { ec ->
                    while (ec.moveToNext()) {
                        val email = ec.getString(0) ?: continue
                        vcfBuilder.appendLine("EMAIL:$email")
                    }
                }

                vcfBuilder.appendLine("END:VCARD")
            }
        }

        val vcf = vcfBuilder.toString()
        return NanoHTTPD.newFixedLengthResponse(
            Response.Status.OK, "text/vcard", vcf
        ).apply {
            addHeader("Content-Disposition", "attachment; filename=\"contacts.vcf\"")
        }
    }

    // ── count ────────────────────────────────────────────────────────────
    private fun countContacts(): Response {
        val cursor = cr.query(
            ContactsContract.Contacts.CONTENT_URI,
            arrayOf(ContactsContract.Contacts._ID),
            null, null, null
        )
        val count = cursor?.count ?: 0
        cursor?.close()
        return jsonOk(mapOf("count" to count))
    }

    // ── import VCF ───────────────────────────────────────────────────────
    private fun importVcf(session: NanoHTTPD.IHTTPSession): Response {
        val body = mutableMapOf<String, String>()
        session.parseBody(body)
        val vcfContent = body["postData"] ?: body["content"] ?: ""

        if (vcfContent.isEmpty()) return jsonError("No VCF data in body")

        // Write to temp file and trigger import intent
        val tmpFile = java.io.File(context.cacheDir, "import_contacts.vcf")
        tmpFile.writeText(vcfContent)

        // Insert contacts via ContentProvider (parse VCF manually for reliability)
        var imported = 0
        val lines = vcfContent.lines()
        var currentName = ""
        var currentPhones = mutableListOf<String>()
        var currentEmails = mutableListOf<String>()

        for (line in lines) {
            when {
                line.startsWith("FN:") -> currentName = line.removePrefix("FN:")
                line.startsWith("TEL") -> {
                    val phone = line.substringAfter(":").trim()
                    if (phone.isNotEmpty()) currentPhones.add(phone)
                }
                line.startsWith("EMAIL") -> {
                    val email = line.substringAfter(":").trim()
                    if (email.isNotEmpty()) currentEmails.add(email)
                }
                line.trim() == "END:VCARD" -> {
                    if (currentName.isNotEmpty()) {
                        insertContact(currentName, currentPhones, currentEmails)
                        imported++
                    }
                    currentName = ""
                    currentPhones = mutableListOf()
                    currentEmails = mutableListOf()
                }
            }
        }

        tmpFile.delete()
        return jsonOk(mapOf("imported" to imported))
    }

    private fun insertContact(name: String, phones: List<String>, emails: List<String>) {
        val ops = ArrayList<android.content.ContentProviderOperation>()
        val rawIdx = ops.size

        ops.add(
            android.content.ContentProviderOperation.newInsert(ContactsContract.RawContacts.CONTENT_URI)
                .withValue(ContactsContract.RawContacts.ACCOUNT_TYPE, null)
                .withValue(ContactsContract.RawContacts.ACCOUNT_NAME, null)
                .build()
        )

        // Name
        ops.add(
            android.content.ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawIdx)
                .withValue(ContactsContract.Data.MIMETYPE,
                    ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE)
                .withValue(ContactsContract.CommonDataKinds.StructuredName.DISPLAY_NAME, name)
                .build()
        )

        // Phones
        for (phone in phones) {
            ops.add(
                android.content.ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                    .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawIdx)
                    .withValue(ContactsContract.Data.MIMETYPE,
                        ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE)
                    .withValue(ContactsContract.CommonDataKinds.Phone.NUMBER, phone)
                    .withValue(ContactsContract.CommonDataKinds.Phone.TYPE,
                        ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE)
                    .build()
            )
        }

        // Emails
        for (email in emails) {
            ops.add(
                android.content.ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                    .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawIdx)
                    .withValue(ContactsContract.Data.MIMETYPE,
                        ContactsContract.CommonDataKinds.Email.CONTENT_ITEM_TYPE)
                    .withValue(ContactsContract.CommonDataKinds.Email.ADDRESS, email)
                    .withValue(ContactsContract.CommonDataKinds.Email.TYPE,
                        ContactsContract.CommonDataKinds.Email.TYPE_HOME)
                    .build()
            )
        }

        try {
            cr.applyBatch(ContactsContract.AUTHORITY, ops)
        } catch (_: Exception) {}
    }
}

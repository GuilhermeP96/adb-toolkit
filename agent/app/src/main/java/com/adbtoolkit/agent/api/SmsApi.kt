package com.adbtoolkit.agent.api

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.Telephony
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONArray
import org.json.JSONObject

/**
 * SMS API — direct ContentResolver access.
 *
 * Endpoints:
 *   GET  /api/sms/list?limit=500&offset=0
 *   GET  /api/sms/export                  → full JSON export
 *   GET  /api/sms/count
 *   POST /api/sms/import                  → body = JSON array of SMS
 *   GET  /api/sms/conversations
 */
class SmsApi(private val context: Context) {

    private val cr: ContentResolver get() = context.contentResolver

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""

        return when (action) {
            "list"          -> listSms(session.parms)
            "export"        -> exportAll()
            "count"         -> count()
            "import"        -> importSms(session)
            "conversations" -> conversations()
            else -> jsonError("Unknown sms action: $action")
        }
    }

    // ── list ─────────────────────────────────────────────────────────────
    private fun listSms(params: Map<String, String>): Response {
        val limit = params["limit"]?.toIntOrNull() ?: 500
        val offset = params["offset"]?.toIntOrNull() ?: 0

        val messages = queryMessages(limit, offset)
        return jsonOk(JSONObject().apply {
            put("count", messages.length())
            put("offset", offset)
            put("limit", limit)
            put("messages", messages)
        })
    }

    // ── export ───────────────────────────────────────────────────────────
    private fun exportAll(): Response {
        val all = queryMessages(limit = Int.MAX_VALUE, offset = 0)
        return jsonOk(JSONObject().apply {
            put("count", all.length())
            put("messages", all)
        })
    }

    // ── count ────────────────────────────────────────────────────────────
    private fun count(): Response {
        val cursor = cr.query(
            Uri.parse("content://sms"),
            arrayOf("_id"),
            null, null, null
        )
        val c = cursor?.count ?: 0
        cursor?.close()
        return jsonOk(mapOf("count" to c))
    }

    // ── conversations ────────────────────────────────────────────────────
    private fun conversations(): Response {
        val threads = JSONArray()
        val cursor = cr.query(
            Uri.parse("content://sms"),
            arrayOf("thread_id", "address", "date", "body", "type"),
            null, null, "date DESC"
        )

        val seen = mutableSetOf<String>()

        cursor?.use { c ->
            while (c.moveToNext()) {
                val threadId = c.getString(0) ?: continue
                if (threadId in seen) continue
                seen.add(threadId)

                threads.put(JSONObject().apply {
                    put("thread_id", threadId)
                    put("address", c.getString(1) ?: "")
                    put("last_date", c.getLong(2))
                    put("last_body", c.getString(3) ?: "")
                    put("type", c.getInt(4))
                })
            }
        }

        return jsonOk(JSONObject().apply {
            put("count", threads.length())
            put("conversations", threads)
        })
    }

    // ── import ───────────────────────────────────────────────────────────
    private fun importSms(session: NanoHTTPD.IHTTPSession): Response {
        val body = mutableMapOf<String, String>()
        session.parseBody(body)
        val data = body["postData"] ?: body["content"] ?: ""

        if (data.isEmpty()) return jsonError("No SMS data in body")

        val arr = JSONArray(data)
        var imported = 0

        for (i in 0 until arr.length()) {
            val sms = arr.getJSONObject(i)
            val address = sms.optString("address", "")
            val smsBody = sms.optString("body", "")
            val date = sms.optLong("date", System.currentTimeMillis())
            val type = sms.optInt("type", 1)  // 1=inbox, 2=sent
            val read = sms.optInt("read", 1)

            if (address.isEmpty() || smsBody.isEmpty()) continue

            val values = android.content.ContentValues().apply {
                put("address", address)
                put("body", smsBody)
                put("date", date)
                put("type", type)
                put("read", read)
            }

            try {
                cr.insert(Uri.parse("content://sms"), values)
                imported++
            } catch (_: Exception) {}
        }

        return jsonOk(mapOf("imported" to imported, "total" to arr.length()))
    }

    // ── helpers ──────────────────────────────────────────────────────────
    private fun queryMessages(limit: Int, offset: Int): JSONArray {
        val messages = JSONArray()

        val cursor = cr.query(
            Uri.parse("content://sms"),
            arrayOf("_id", "thread_id", "address", "date", "body", "type", "read", "seen"),
            null, null,
            "date DESC LIMIT $limit OFFSET $offset"
        )

        cursor?.use { c ->
            while (c.moveToNext()) {
                messages.put(JSONObject().apply {
                    put("id", c.getLong(0))
                    put("thread_id", c.getString(1) ?: "")
                    put("address", c.getString(2) ?: "")
                    put("date", c.getLong(3))
                    put("body", c.getString(4) ?: "")
                    put("type", c.getInt(5))       // 1=received, 2=sent
                    put("read", c.getInt(6))
                    put("seen", c.getInt(7))
                })
            }
        }

        return messages
    }
}

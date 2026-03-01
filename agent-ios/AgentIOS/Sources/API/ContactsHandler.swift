import Foundation
import Contacts

// ─────────────────────────────────────────────────────────────────────
//  ContactsHandler.swift — Contacts API (Contacts.framework)
//
//  Endpoints:
//    GET /api/contacts/list        — all contacts as JSON
//    GET /api/contacts/count       — contact count
//    GET /api/contacts/export-vcf  — VCF 3.0 text stream
//    POST /api/contacts/import-vcf — import VCF data
// ─────────────────────────────────────────────────────────────────────

final class ContactsHandler {

    private let store = CNContactStore()

    func handle(method: String, parts: [String], request: HTTPRequest) -> HTTPResponse {
        let action = parts.first ?? "list"

        switch action {
        case "list":     return listContacts()
        case "count":    return contactCount()
        case "export-vcf": return exportVCF()
        case "import-vcf": return importVCF(request: request)
        default:
            return .error("Unknown contacts action: \(action)", status: .notFound)
        }
    }

    // MARK: - List all contacts

    private func listContacts() -> HTTPResponse {
        let keys: [CNKeyDescriptor] = [
            CNContactGivenNameKey as CNKeyDescriptor,
            CNContactFamilyNameKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactNoteKey as CNKeyDescriptor,
            CNContactImageDataAvailableKey as CNKeyDescriptor,
        ]

        var contacts: [[String: Any]] = []
        let fetchRequest = CNContactFetchRequest(keysToFetch: keys)
        fetchRequest.sortOrder = .givenName

        do {
            try store.enumerateContacts(with: fetchRequest) { contact, _ in
                var c: [String: Any] = [
                    "id": contact.identifier,
                    "given_name": contact.givenName,
                    "family_name": contact.familyName,
                    "display_name": "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces),
                    "organization": contact.organizationName,
                    "has_photo": contact.imageDataAvailable,
                ]

                c["phones"] = contact.phoneNumbers.map { phone in
                    [
                        "label": CNLabeledValue<NSString>.localizedString(forLabel: phone.label ?? ""),
                        "number": phone.value.stringValue,
                    ]
                }

                c["emails"] = contact.emailAddresses.map { email in
                    [
                        "label": CNLabeledValue<NSString>.localizedString(forLabel: email.label ?? ""),
                        "address": email.value as String,
                    ]
                }

                contacts.append(c)
            }

            return .ok(["contacts": contacts, "count": contacts.count])
        } catch {
            return .error("Failed to fetch contacts: \(error.localizedDescription)")
        }
    }

    // MARK: - Count

    private func contactCount() -> HTTPResponse {
        let keys: [CNKeyDescriptor] = [CNContactIdentifierKey as CNKeyDescriptor]
        let fetchRequest = CNContactFetchRequest(keysToFetch: keys)
        var count = 0

        do {
            try store.enumerateContacts(with: fetchRequest) { _, _ in
                count += 1
            }
            return .ok(["count": count])
        } catch {
            return .error("Failed to count contacts: \(error.localizedDescription)")
        }
    }

    // MARK: - Export VCF

    private func exportVCF() -> HTTPResponse {
        let keys: [CNKeyDescriptor] = [CNContactVCardSerialization.descriptorForRequiredKeys()]
        let fetchRequest = CNContactFetchRequest(keysToFetch: keys)
        var allContacts: [CNContact] = []

        do {
            try store.enumerateContacts(with: fetchRequest) { contact, _ in
                allContacts.append(contact)
            }

            let vcfData = try CNContactVCardSerialization.data(with: allContacts)
            return .binary(data: vcfData, contentType: "text/vcard", filename: "contacts.vcf")
        } catch {
            return .error("Failed to export VCF: \(error.localizedDescription)")
        }
    }

    // MARK: - Import VCF

    private func importVCF(request: HTTPRequest) -> HTTPResponse {
        guard !request.body.isEmpty else {
            return .error("Empty VCF data")
        }

        do {
            let contacts = try CNContactVCardSerialization.contacts(with: request.body)
            let saveRequest = CNSaveRequest()
            var imported = 0

            for contact in contacts {
                let mutable = contact.mutableCopy() as! CNMutableContact
                saveRequest.add(mutable, toContainerWithIdentifier: nil)
                imported += 1
            }

            try store.execute(saveRequest)
            return .ok(["imported": imported])
        } catch {
            return .error("Failed to import VCF: \(error.localizedDescription)")
        }
    }
}

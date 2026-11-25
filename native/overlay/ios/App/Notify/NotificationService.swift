import UserNotifications

// Puts a question's own options on its notification's buttons (docs/design/native-app.md).
// iOS draws only the actions of a category registered beforehand, with fixed titles, so each
// push that offers "actions" gets a category made for it here, before it is shown. If this
// fails or runs out of time the push is shown as sent: no buttons, and a tap opens the pane.
class NotificationService: UNNotificationServiceExtension {
    override func didReceive(_ request: UNNotificationRequest, withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        let offered = (request.content.userInfo["actions"] as? [[String: String]] ?? []).compactMap { item in
            item["action"].flatMap { id in item["title"].map { (id: id, title: $0) } }
        }
        guard !offered.isEmpty, let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            return contentHandler(request.content)
        }
        // Named by its titles, so the same options share one category. The app delegate
        // answers by the action's identifier ("answer:<index>"), and waits for the unlock.
        let category = UNNotificationCategory(
            identifier: offered.map(\.title).joined(separator: "\n"),
            actions: offered.map { UNNotificationAction(identifier: $0.id, title: $0.title, options: .authenticationRequired) },
            intentIdentifiers: [])
        content.categoryIdentifier = category.identifier
        // Categories belong to the whole app: keep those a notification still on screen
        // uses, and drop the rest so the set cannot grow.
        let center = UNUserNotificationCenter.current()
        center.getDeliveredNotifications { delivered in
            let shown = Set(delivered.map(\.request.content.categoryIdentifier))
            center.getNotificationCategories { known in
                center.setNotificationCategories(known.filter { shown.contains($0.identifier) }.union([category]))
                // Read back before delivering: the registration is queued behind this
                // read, so the category exists by the time iOS looks for it.
                center.getNotificationCategories { _ in contentHandler(content) }
            }
        }
    }
}

import Capacitor
import UIKit
import UserNotifications
import WebKit

// Replaces the Capacitor template's AppDelegate (docs/design/native-app.md). Everything the
// web app cannot do lives here: the device token, and a question answered from its
// notification while no page is running to answer it.

#if DEBUG
private let apnsEnvironment = "development" // a debug build is signed for the APNs sandbox
#else
private let apnsEnvironment = "production"
#endif
private let daemonKey = "daemon" // the page that registered for push: where answers are sent

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // The delegate has to be in place before launch finishes: iOS starts the app in the
        // background to deliver an action, with no scene, so no webview and no Capacitor.
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        // iOS draws only actions registered ahead of time, so their titles cannot be the
        // question's own options: a push names "answer1" or "answer2" by how many it offers
        // and lists them in its body.
        center.setNotificationCategories(Set((1...2).map { count in
            UNNotificationCategory(identifier: "answer\(count)", actions: (0..<count).map {
                // Typing into a terminal from a locked phone waits for the unlock.
                UNNotificationAction(identifier: "answer:\($0)", title: "Option \($0 + 1)", options: .authenticationRequired)
            }, intentIdentifiers: [])
        }))
        return true
    }

    func application(_ application: UIApplication, configurationForConnecting connectingSceneSession: UISceneSession, options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }

    private var bridge: CAPBridgeViewController? {
        let windows = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        return windows.compactMap { $0.rootViewController as? CAPBridgeViewController }.first
    }

    // The push plugin hands this string to the page, which posts it to the daemon (web/push.js).
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        UserDefaults.standard.set(bridge?.webView?.url, forKey: daemonKey)
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        NotificationCenter.default.post(name: .capacitorDidRegisterForRemoteNotifications, object: "\(apnsEnvironment):\(token)")
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NotificationCenter.default.post(name: .capacitorDidFailToRegisterForRemoteNotifications, object: error)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound]) // another pane's question shows while the app is open
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let action = response.actionIdentifier.split(separator: ":")
        guard action.count == 2, action[0] == "answer", let index = Int(action[1]), let nonce = info["nonce"] as? String,
              let url = UserDefaults.standard.url(forKey: daemonKey).flatMap({ URL(string: "/api/push/answer", relativeTo: $0) }) else {
            // A tap on the notification itself: the page follows its link (web/push.js). Handed
            // straight to the push plugin, because Capacitor's router would drop the tap on
            // the "Answer not sent" notice below, which no push delivered.
            let controller = bridge
            controller?.loadViewIfNeeded()
            controller?.bridge?.notificationRouter.pushNotificationHandler?.didReceive(response: response)
            return completionHandler()
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["nonce": nonce, "option_index": index])
        // The login in front of the daemon is a cookie the webview holds.
        WKWebsiteDataStore.default().httpCookieStore.getAllCookies { cookies in
            let configuration = URLSessionConfiguration.ephemeral
            cookies.forEach { configuration.httpCookieStorage?.setCookie($0) }
            URLSession(configuration: configuration).dataTask(with: request) { _, reply, _ in
                // An expired login redirects to its sign-in page, which answers 200 from elsewhere.
                if (reply as? HTTPURLResponse)?.statusCode != 200 || reply?.url?.absoluteString != url.absoluteString {
                    let content = UNMutableNotificationContent()
                    content.title = "Answer not sent"
                    content.body = "Tap to open the session and answer there."
                    content.userInfo = info
                    center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
                }
                completionHandler()
            }.resume()
        }
    }
}

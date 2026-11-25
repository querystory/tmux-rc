import Capacitor
import UIKit
import UserNotifications
import WebKit

// Replaces the Capacitor template's AppDelegate (docs/design/native-app.md). Everything the
// web app cannot do lives here: the device token, a question answered from its notification
// while no page is running to answer it, and the rule for which pages may call native code.

// Which of Apple's push hosts this build's token belongs to; the daemon's push to the other
// is refused and the token dropped. The signature decides it, not the build configuration: a
// build from Xcode carries its provisioning profile, and a TestFlight or App Store build,
// which is always production, carries none.
private let apnsEnvironment: String = {
    #if targetEnvironment(simulator)
    return "development"
    #else
    let profile = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision").flatMap { try? String(contentsOf: $0, encoding: .isoLatin1) }
    let sandbox = profile?.range(of: #"aps-environment</key>\s*<string>development"#, options: .regularExpression)
    return sandbox == nil ? "production" : "development"
    #endif
}()
private let daemonKey = "daemon" // the address the bundled page opened: the one daemon

// The webview may be taken anywhere (a login page, a link followed in place), and Capacitor's
// bridge goes with it, with its built-in HTTP and cookie plugins: enough to act as the user
// on the daemon. So only the bundled page and the daemon it opened are answered, on both of
// the bridge's ways in: script messages, and the prompt() its cookie calls ride on.
// Everything else Capacitor's handler does as the webview's delegate passes straight through.
final class BridgeGate: NSObject, WKScriptMessageHandler, WKUIDelegate {
    private let capacitor: WebViewDelegationHandler
    init(_ capacitor: WebViewDelegationHandler) { self.capacitor = capacitor }

    private func trusted(_ frame: WKFrameInfo) -> Bool {
        let origin = frame.securityOrigin, daemon = UserDefaults.standard.url(forKey: daemonKey)
        return origin.protocol == "capacitor" || origin.protocol == "https" && origin.host == daemon?.host && origin.port == daemon?.port ?? 0
    }

    override func responds(to selector: Selector!) -> Bool { super.responds(to: selector) || capacitor.responds(to: selector) }
    override func forwardingTarget(for selector: Selector!) -> Any? { capacitor }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if trusted(message.frameInfo) { capacitor.userContentController(userContentController, didReceive: message) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        guard trusted(frame) else { return completionHandler(nil) }
        capacitor.webView(webView, runJavaScriptTextInputPanelWithPrompt: prompt, defaultText: defaultText, initiatedByFrame: frame, completionHandler: completionHandler)
    }
}

class ShellViewController: CAPBridgeViewController {
    private var gate: BridgeGate? // the webview holds its delegate weakly

    override func capacitorDidLoad() {
        guard let capacitor = (bridge as? CapacitorBridge)?.webViewDelegationHandler else { return }
        let gate = BridgeGate(capacitor), scripts = webView?.configuration.userContentController
        self.gate = gate
        scripts?.removeScriptMessageHandler(forName: "bridge")
        scripts?.add(gate, name: "bridge")
        webView?.uiDelegate = gate
        NotificationCenter.default.addObserver(forName: .capacitorDecidePolicyForNavigationAction, object: nil, queue: .main) { note in
            // Only the bundled page names the daemon: it is where that page sends the webview.
            guard let action = note.object as? WKNavigationAction, action.targetFrame?.isMainFrame == true,
                  let source = action.sourceFrame as WKFrameInfo?, source.securityOrigin.protocol == "capacitor",
                  let url = action.request.url, url.scheme == "https" else { return }
            let old = UserDefaults.standard.url(forKey: daemonKey)
            if old?.host != url.host || old?.port != url.port {
                // Another daemon's questions cannot be answered at this one.
                UNUserNotificationCenter.current().removeAllDeliveredNotifications()
            }
            UserDefaults.standard.set(url, forKey: daemonKey)
        }
        if let tap = AppDelegate.unopened { AppDelegate.unopened = nil; open(tap) }
    }

    // A tap on a notification: the page follows its link (web/push.js). Handed straight to
    // the push plugin, because Capacitor's router would drop a tap on the app's own "Answer
    // not sent" notice, which no push delivered.
    func open(_ tap: UNNotificationResponse) {
        bridge?.notificationRouter.pushNotificationHandler?.didReceive(response: tap)
    }
}

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // The delegate has to be in place before launch finishes: iOS starts the app in the
        // background to deliver an action, with no scene, so no webview and no Capacitor.
        // The actions themselves are registered per push, by the Notify extension.
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, configurationForConnecting connectingSceneSession: UISceneSession, options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }

    static var unopened: UNNotificationResponse? // a tap that came before there was a webview to open it in

    private var shell: ShellViewController? {
        let windows = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        return windows.compactMap { $0.rootViewController as? ShellViewController }.first
    }

    // The push plugin hands this string to the page, which posts it to the daemon (web/push.js).
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
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
            // A tap on the notification itself. On a cold start it can arrive before the
            // window does, and then waits for the shell to load.
            if let shell { shell.loadViewIfNeeded(); shell.open(response) } else { Self.unopened = response }
            return completionHandler()
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["nonce": nonce, "option_index": index])
        // The login in front of the daemon is a cookie the webview holds. No Origin is sent:
        // the daemon admits a write without one, and could never list an app's origin.
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

import AppKit
import Foundation

private let configRelativePath = "Library/Application Support/EnterEscHost/config.json"
private let stateRelativePath = "Library/Application Support/EnterEscHost/state.json"
private let logRelativePath = "Library/Logs/EnterEscHost/menu-bar.log"

final class ConfigStore {
    let url: URL

    init(url: URL) {
        self.url = url
    }

    func load() -> [String: Any] {
        guard let data = try? Data(contentsOf: url) else {
            return [:]
        }

        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let dictionary = object as? [String: Any]
        else {
            return [:]
        }

        return dictionary
    }

    func save(_ dictionary: [String: Any]) throws {
        let directory = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: dictionary, options: [.prettyPrinted, .sortedKeys])
        var text = String(decoding: data, as: UTF8.self)
        if !text.hasSuffix("\n") {
            text += "\n"
        }
        try text.write(to: url, atomically: true, encoding: .utf8)
    }

    func bool(forKey key: String, default fallback: Bool = false) -> Bool {
        let value = load()[key]
        if let boolValue = value as? Bool {
            return boolValue
        }
        if let intValue = value as? Int {
            return intValue != 0
        }
        if let stringValue = value as? String {
            switch stringValue.lowercased() {
            case "1", "true", "yes", "on":
                return true
            case "0", "false", "no", "off":
                return false
            default:
                break
            }
        }
        return fallback
    }

    func string(forKey key: String, default fallback: String = "") -> String {
        load()[key] as? String ?? fallback
    }

    @discardableResult
    func set(_ value: Any, forKey key: String) throws -> [String: Any] {
        var dictionary = load()
        dictionary[key] = value
        try save(dictionary)
        return dictionary
    }
}

struct RuntimeStateSnapshot {
    let status: String
    let connectedDevice: String
    let recording: Bool
    let lastConfigChange: String
    let lastError: String

    static let empty = RuntimeStateSnapshot(
        status: "idle",
        connectedDevice: "",
        recording: false,
        lastConfigChange: "",
        lastError: ""
    )
}

final class StateStore {
    let url: URL

    init(url: URL) {
        self.url = url
    }

    func load() -> RuntimeStateSnapshot {
        guard let data = try? Data(contentsOf: url) else {
            return .empty
        }

        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let dictionary = object as? [String: Any]
        else {
            return .empty
        }

        return RuntimeStateSnapshot(
            status: dictionary["status"] as? String ?? "idle",
            connectedDevice: dictionary["connected_device"] as? String ?? "",
            recording: dictionary["recording"] as? Bool ?? false,
            lastConfigChange: dictionary["last_config_change"] as? String ?? "",
            lastError: dictionary["last_error"] as? String ?? ""
        )
    }
}

final class HostProcessController: @unchecked Sendable {
    enum State: String {
        case stopped = "Stopped"
        case starting = "Starting"
        case running = "Running"
        case failed = "Failed"
    }

    private(set) var state: State = .stopped
    private(set) var lastError: String = ""
    private var process: Process?

    let repoRoot: URL
    let configURL: URL
    let logURL: URL

    init(repoRoot: URL, configURL: URL, logURL: URL) {
        self.repoRoot = repoRoot
        self.configURL = configURL
        self.logURL = logURL
    }

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start() {
        guard !isRunning else {
            return
        }

        state = .starting
        lastError = ""

        let scriptURL = repoRoot.appendingPathComponent("host/mac_record_control.py")
        let task = Process()
        task.currentDirectoryURL = repoRoot
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = [
            "python3",
            scriptURL.path,
            "--no-tui",
            "--input-device",
            "default",
            "--config-file",
            configURL.path,
        ]

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        task.environment = environment

        let outPipe = Pipe()
        let errPipe = Pipe()
        task.standardOutput = outPipe
        task.standardError = errPipe

        let outputHandler: (FileHandle) -> Void = { [weak self] handle in
            guard let self else { return }
            handle.readabilityHandler = { readable in
                let data = readable.availableData
                guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                    return
                }
                self.appendLog(text)
            }
        }

        outputHandler(outPipe.fileHandleForReading)
        outputHandler(errPipe.fileHandleForReading)

        task.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self else { return }
                self.process = nil
                if finished.terminationStatus == 0 {
                    self.state = .stopped
                    self.lastError = ""
                } else {
                    self.state = .failed
                    self.lastError = "Exit \(finished.terminationStatus)"
                }
                NotificationCenter.default.post(name: .hostProcessDidChange, object: self)
            }
        }

        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try task.run()
            process = task
            state = .running
            appendLog("[menu] Host process started\n")
        } catch {
            process = nil
            state = .failed
            lastError = error.localizedDescription
            appendLog("[menu] Failed to start host process: \(error.localizedDescription)\n")
        }

        NotificationCenter.default.post(name: .hostProcessDidChange, object: self)
    }

    func stop() {
        guard let process else {
            state = .stopped
            NotificationCenter.default.post(name: .hostProcessDidChange, object: self)
            return
        }

        appendLog("[menu] Stopping host process\n")
        process.terminate()
        self.process = nil
        state = .stopped
        NotificationCenter.default.post(name: .hostProcessDidChange, object: self)
    }

    func restart() {
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.start()
        }
    }

    private func appendLog(_ text: String) {
        let prefix = "[\(Self.timestamp())] "
        let lines = text
            .split(whereSeparator: \.isNewline)
            .map { prefix + $0 }
            .joined(separator: "\n")

        guard !lines.isEmpty else {
            return
        }

        let payload = lines + "\n"
        let data = Data(payload.utf8)

        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if FileManager.default.fileExists(atPath: logURL.path) {
                let handle = try FileHandle(forWritingTo: logURL)
                defer { try? handle.close() }
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
            } else {
                try data.write(to: logURL)
            }
        } catch {
            lastError = "Log write failed: \(error.localizedDescription)"
        }
    }

    private static func timestamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: Date())
    }
}

extension Notification.Name {
    static let hostProcessDidChange = Notification.Name("HostProcessDidChange")
}

@MainActor
final class MenuBarController: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()

    private let configStore: ConfigStore
    private let stateStore: StateStore
    private let hostController: HostProcessController
    private var refreshTimer: Timer?

    private let hostStateItem = NSMenuItem(title: "Host: Unknown", action: nil, keyEquivalent: "")
    private let runtimeStateItem = NSMenuItem(title: "Runtime: Unknown", action: nil, keyEquivalent: "")
    private let recordingItem = NSMenuItem(title: "Recording: no", action: nil, keyEquivalent: "")
    private let configPathItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let lastErrorItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let startItem = NSMenuItem(title: "Start Host", action: #selector(startHost), keyEquivalent: "")
    private let stopItem = NSMenuItem(title: "Stop Host", action: #selector(stopHost), keyEquivalent: "")
    private let restartItem = NSMenuItem(title: "Restart Host", action: #selector(restartHost), keyEquivalent: "")
    private let translationItem = NSMenuItem(title: "Translate To English", action: #selector(toggleTranslation), keyEquivalent: "")
    private let pressReturnItem = NSMenuItem(title: "Press Return", action: #selector(togglePressReturn), keyEquivalent: "")
    private let openConfigItem = NSMenuItem(title: "Open Config File", action: #selector(openConfigFile), keyEquivalent: "")
    private let openStateItem = NSMenuItem(title: "Open Runtime State", action: #selector(openStateFile), keyEquivalent: "")
    private let openConfigFolderItem = NSMenuItem(title: "Open Config Folder", action: #selector(openConfigFolder), keyEquivalent: "")
    private let openLogItem = NSMenuItem(title: "Open Host Log", action: #selector(openLogFile), keyEquivalent: "")
    private let quitItem = NSMenuItem(title: "Quit", action: #selector(quitApp), keyEquivalent: "q")

    init(configStore: ConfigStore, stateStore: StateStore, hostController: HostProcessController) {
        self.configStore = configStore
        self.stateStore = stateStore
        self.hostController = hostController
        super.init()
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(refreshMenu),
            name: .hostProcessDidChange,
            object: nil
        )
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        setupMenu()
        hostController.start()
        refreshTimer = Timer.scheduledTimer(
            timeInterval: 1.0,
            target: self,
            selector: #selector(refreshMenu),
            userInfo: nil,
            repeats: true
        )
        refreshMenu()
    }

    private func setupMenu() {
        if let button = statusItem.button {
            button.title = "EnterEsc"
        }

        hostStateItem.isEnabled = false
        runtimeStateItem.isEnabled = false
        recordingItem.isEnabled = false
        configPathItem.isEnabled = false
        lastErrorItem.isEnabled = false

        startItem.target = self
        stopItem.target = self
        restartItem.target = self
        translationItem.target = self
        pressReturnItem.target = self
        openConfigItem.target = self
        openStateItem.target = self
        openConfigFolderItem.target = self
        openLogItem.target = self
        quitItem.target = self

        menu.addItem(hostStateItem)
        menu.addItem(runtimeStateItem)
        menu.addItem(recordingItem)
        menu.addItem(configPathItem)
        menu.addItem(lastErrorItem)
        menu.addItem(.separator())
        menu.addItem(startItem)
        menu.addItem(stopItem)
        menu.addItem(restartItem)
        menu.addItem(.separator())
        menu.addItem(translationItem)
        menu.addItem(pressReturnItem)
        menu.addItem(.separator())
        menu.addItem(openConfigItem)
        menu.addItem(openStateItem)
        menu.addItem(openConfigFolderItem)
        menu.addItem(openLogItem)
        menu.addItem(.separator())
        menu.addItem(quitItem)

        statusItem.menu = menu
    }

    @objc private func refreshMenu() {
        let translateEnabled = configStore.bool(forKey: "translate_to_en")
        let pressReturnEnabled = configStore.bool(forKey: "press_return")
        let deviceName = configStore.string(forKey: "device_name", default: "EnterEsc Seeed")
        let runtimeState = stateStore.load()

        hostStateItem.title = "Host: \(hostController.state.rawValue)\(hostController.lastError.isEmpty ? "" : " (\(hostController.lastError))")"
        runtimeStateItem.title = "Runtime: \(runtimeState.status) | Connected: \(runtimeState.connectedDevice.isEmpty ? "-" : runtimeState.connectedDevice)"
        recordingItem.title = "Recording: \(runtimeState.recording ? "yes" : "no")"
        configPathItem.title = "Device: \(deviceName)"
        lastErrorItem.title = runtimeState.lastError.isEmpty ? "Last error: -" : "Last error: \(runtimeState.lastError)"
        lastErrorItem.isHidden = runtimeState.lastError.isEmpty
        translationItem.state = translateEnabled ? .on : .off
        pressReturnItem.state = pressReturnEnabled ? .on : .off
        startItem.isEnabled = !hostController.isRunning
        stopItem.isEnabled = hostController.isRunning
        restartItem.isEnabled = hostController.isRunning

        if let button = statusItem.button {
            if runtimeState.recording {
                button.title = "EnterEsc REC"
            } else if translateEnabled {
                button.title = "EnterEsc EN"
            } else {
                button.title = "EnterEsc"
            }
        }
    }

    @objc private func startHost() {
        hostController.start()
        refreshMenu()
    }

    @objc private func stopHost() {
        hostController.stop()
        refreshMenu()
    }

    @objc private func restartHost() {
        hostController.restart()
        refreshMenu()
    }

    @objc private func toggleTranslation() {
        let newValue = !configStore.bool(forKey: "translate_to_en")
        _ = try? configStore.set(newValue, forKey: "translate_to_en")
        refreshMenu()
    }

    @objc private func togglePressReturn() {
        let newValue = !configStore.bool(forKey: "press_return")
        _ = try? configStore.set(newValue, forKey: "press_return")
        refreshMenu()
    }

    @objc private func openConfigFile() {
        ensureConfigFile()
        NSWorkspace.shared.open(configStore.url)
    }

    @objc private func openStateFile() {
        ensureStateFile()
        NSWorkspace.shared.open(stateStore.url)
    }

    @objc private func openConfigFolder() {
        ensureConfigFile()
        NSWorkspace.shared.open(configStore.url.deletingLastPathComponent())
    }

    @objc private func openLogFile() {
        NSWorkspace.shared.open(hostController.logURL)
    }

    @objc private func quitApp() {
        refreshTimer?.invalidate()
        hostController.stop()
        NSApp.terminate(nil)
    }

    private func ensureConfigFile() {
        if FileManager.default.fileExists(atPath: configStore.url.path) {
            return
        }
        try? configStore.save([:])
    }

    private func ensureStateFile() {
        if FileManager.default.fileExists(atPath: stateStore.url.path) {
            return
        }
        try? FileManager.default.createDirectory(
            at: stateStore.url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let payload = """
        {
          "connected_device" : "",
          "last_config_change" : "",
          "last_error" : "",
          "recording" : false,
          "status" : "idle"
        }
        """
        try? payload.write(to: stateStore.url, atomically: true, encoding: .utf8)
    }
}

func defaultConfigURL() -> URL {
    FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(configRelativePath)
}

func defaultLogURL() -> URL {
    FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(logRelativePath)
}

func defaultStateURL() -> URL {
    FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(stateRelativePath)
}

func resolveRepoRoot() -> URL {
    if let explicit = ProcessInfo.processInfo.environment["ENTER_ESC_REPO_ROOT"], !explicit.isEmpty {
        return URL(fileURLWithPath: explicit)
    }

    let currentDirectory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    let sourcePath = URL(fileURLWithPath: #filePath)
    let sourceDerivedRoot = sourcePath
        .deletingLastPathComponent()   // EnterEscMenuBar
        .deletingLastPathComponent()   // Sources
        .deletingLastPathComponent()   // EnterEscMenuBar package root
        .deletingLastPathComponent()   // macos

    let candidates = [currentDirectory, sourceDerivedRoot]
    for candidate in candidates {
        let scriptPath = candidate.appendingPathComponent("host/mac_record_control.py").path
        if FileManager.default.fileExists(atPath: scriptPath) {
            return candidate
        }
    }

    return currentDirectory
}

let configStore = ConfigStore(url: defaultConfigURL())
let stateStore = StateStore(url: defaultStateURL())
let hostController = HostProcessController(
    repoRoot: resolveRepoRoot(),
    configURL: defaultConfigURL(),
    logURL: defaultLogURL()
)

let app = NSApplication.shared
let delegate = MenuBarController(configStore: configStore, stateStore: stateStore, hostController: hostController)
app.delegate = delegate
app.run()

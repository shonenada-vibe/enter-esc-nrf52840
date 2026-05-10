// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "EnterEscMenuBar",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "EnterEscMenuBar", targets: ["EnterEscMenuBar"]),
    ],
    targets: [
        .executableTarget(
            name: "EnterEscMenuBar",
            path: "Sources/EnterEscMenuBar"
        ),
    ]
)

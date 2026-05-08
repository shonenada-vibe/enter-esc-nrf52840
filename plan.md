# macOS Background Menu Bar App Plan

## Goal

Turn `host/mac_record_control.py` from a terminal-first TUI into a macOS background app that:

- runs without keeping a terminal window open
- stays available from the macOS menu bar
- lets the user change runtime config from the menu bar
- applies config changes immediately where practical
- keeps BLE recording, STT, translation, and text typing behavior intact

The first concrete UX target is:

- launch as a menu bar app
- show current connection / recording / error state
- allow toggling translation on or off from the menu bar

## Non-Goals For Phase 1

- full Settings window with polished macOS-native forms
- App Store packaging / notarization
- multi-device support
- background auto-update
- iCloud / sync / account system

## Current State

The current host script already has useful building blocks:

- runtime config object with live updates
- background BLE loop
- recorder / STT / translator abstractions
- state snapshot model
- log buffer
- terminal TUI on top of the runtime service

This is a good base, but the terminal UI is still tightly bundled inside one Python process and one script.

## Recommended Target Architecture

Split the host app into 3 layers:

1. Core service layer
- BLE connection management
- recording lifecycle
- transcription
- translation
- typing to active app
- runtime config store
- state store
- structured logs

2. UI adapter layer
- menu bar UI actions
- settings presentation
- log viewing
- start / stop / reconnect commands

3. packaging / app runtime layer
- macOS app bundle entry point
- background startup behavior
- permissions guidance
- persistence of config on disk

The key rule is:

- the service layer must not depend on `curses`
- the menu bar UI must call into the service layer through a narrow API

## Technology Decision

### Option A: Keep Python and add a menu bar shell

Possible libraries:

- `rumps`
- `pyobjc`

Pros:

- reuses most current Python code
- fastest migration path
- lower rewrite cost

Cons:

- macOS integration is less clean than a native Swift app
- packaging and long-term maintenance are more fragile
- permission handling and background behavior are more awkward

### Option B: Rewrite as a native Swift macOS app

Possible approach:

- Swift / AppKit or SwiftUI menu bar app
- BLE, audio capture, config, logging, and menu bar UI all live in the macOS app
- STT / translation HTTP calls can remain direct network clients from Swift

Pros:

- better macOS UX
- cleaner menu bar behavior
- better permission handling
- easier packaging, codesigning, login items, and long-term maintenance

Cons:

- higher rewrite cost
- macOS-only implementation
- requires rebuilding audio / BLE / config logic outside Python

### Option C: Rewrite service logic in Rust and use a native macOS shell

Possible approach:

- Rust core for BLE/session/config/logging/runtime state
- Swift menu bar app for macOS UI
- Swift calls Rust through FFI or a local IPC boundary

Pros:

- stronger long-term core architecture
- safer concurrency model for the runtime service
- reusable core if another desktop shell is added later

Cons:

- highest implementation complexity
- more moving parts than Swift-only
- FFI / IPC design cost arrives immediately

### Updated recommendation

Treat **Swift and Rust as first-class migration targets**, not just Python extensions.

Recommended execution order:

1. do a short architecture spike comparing Swift-only vs Rust-core-plus-Swift-shell
2. keep Python only as a temporary reference implementation
3. avoid investing heavily in more Python UI work beyond what is needed to preserve current behavior during migration

Reason:

- the product target is explicitly macOS background behavior and menu bar control
- native macOS integration matters more now than terminal portability
- the current Python code is a useful behavior reference, but not necessarily the best long-term runtime

### Selection guidance

Choose **Swift-only** if:

- the target is macOS only
- fastest path to a polished menu bar app matters most
- the runtime complexity remains moderate

Choose **Rust + Swift** if:

- you expect the background service to grow substantially
- runtime robustness and isolation are top priorities
- you may later want a non-macOS shell or a reusable daemon core

Do **not** choose “Python menu bar app as the main final architecture” unless the spike shows the native rewrite cost is unjustifiably high.

## Execution Phases

## Phase 0: Architecture Spike

Objective:

- decide whether the rewrite target is Swift-only or Rust core plus Swift shell

Tasks:

- identify Python modules that map directly to future runtime components
- document which responsibilities belong in:
  - menu bar UI
  - background runtime service
  - persistence / secrets / logs
- compare implementation effort for:
  - Swift BLE + Swift audio capture + Swift config
  - Rust runtime + Swift shell
- choose process model:
  - single app process
  - embedded runtime
  - helper daemon / local IPC
- define migration criteria for “enough to stop extending Python”

Deliverable:

- architecture decision record in repo
- selected rewrite path: Swift-only or Rust+Swift

## Phase 1: Refactor For Separation

Objective:

- turn the Python script into a migration reference and test oracle

Tasks:

- move config/state/log/runtime code into `host/app_core.py`
- move BLE/recording/transcription workflow into a service class module
- keep current CLI/TUI as a thin adapter on top of the service
- define a small control surface:
  - start
  - stop
  - reconnect
  - update config field
  - get state snapshot
  - get recent logs

Deliverable:

- current TUI still works
- core runtime can be started without `curses`
- rewrite target has a clear functional spec to match

## Phase 2: Add Persistent Config

Objective:

- make config survive restarts and editable outside terminal args

Tasks:

- define a config file path, e.g. `~/Library/Application Support/EnterEsc/config.json`
- load config at startup
- merge defaults + file values + optional CLI overrides
- save config after runtime edits
- store sensitive fields carefully:
  - preferred: move API keys to Keychain later
  - short term: allow file storage, but document the risk

Deliverable:

- changing translation toggle survives relaunch

## Phase 3: Native Rewrite Skeleton

Objective:

- stand up the new native application/runtime shell

Tasks:

- if Swift-only:
  - create menu bar app shell
  - implement config store
  - implement app state store
  - wire basic menus and lifecycle
- if Rust+Swift:
  - create Rust runtime crate
  - define FFI or IPC interface
  - create Swift menu bar shell
  - wire config/state exchange between shell and runtime

Deliverable:

- app launches from macOS as a menu bar process
- config/state plumbing exists even if BLE/audio behavior is still stubbed

## Phase 4: Rebuild BLE / Recording / STT Pipeline

Objective:

- port the current end-to-end behavior out of Python

Tasks:

- BLE scan/connect/subscribe
- audio capture
- file or buffer handoff to STT
- translation toggle
- paste/type into active app
- parity validation against Python behavior

Deliverable:

- native runtime can perform record -> transcribe -> optional translate -> type

## Phase 5: Add Settings UI

Objective:

- edit all practical runtime parameters from the macOS app

Tasks:

- build a small settings window or popover
- editable fields:
  - BLE device name
  - char UUID
  - STT provider
  - model
  - language
  - translation model
  - input device
  - sample rate
  - channels
  - press-return
- input device picker should enumerate system devices directly
- mark which fields require reconnect
- validate fields before applying

Deliverable:

- most parameters are configurable from GUI and apply immediately or on reconnect

## Phase 6: Logging and Diagnostics

Objective:

- make the background app debuggable without terminal output

Tasks:

- replace print-style logging with structured logger methods
- keep in-memory recent logs
- write rolling log file under `~/Library/Logs/...`
- add menu item to open logs
- surface recent translation requests/responses in a debug section
- clearly show last error in UI

Deliverable:

- user can inspect failures without launching from terminal

## Phase 7: Permissions and Background Runtime

Objective:

- make it behave like a real macOS background app

Tasks:

- document required permissions:
  - microphone
  - accessibility
  - possibly Bluetooth
- detect common permission failures and show actionable error text
- support launch at login later if desired
- verify behavior when no terminal is attached

Deliverable:

- app works after double-click launch and reports permission problems clearly

## Phase 8: Packaging

Objective:

- produce something installable and repeatable

Tasks:

- choose packager, likely `py2app` or `briefcase`
- create app bundle entry point
- bundle Python dependencies
- test local launch, quit, relaunch, permission prompts
- optionally add a development Make target for packaging

Deliverable:

- `.app` bundle runnable on the target Mac

## Concrete Task Breakdown

## Workstream A: Core Refactor

- extract reusable modules from `mac_record_control.py`
- remove `curses` dependencies from the service layer
- define a thread-safe command/config API
- make logs structured and UI-agnostic

## Workstream B: Menu Bar Shell

- choose Swift-only vs Rust+Swift shell structure
- prototype menu bar icon and menu items
- bind menu events to config updates
- add live status refresh timer

## Workstream C: Config Persistence

- config schema
- load/save implementation
- migration logic for future schema changes
- secure handling plan for secrets

## Workstream D: Packaging

- app entry script
- dependency bundling
- app icon and metadata
- local install instructions

## Workstream E: Rewrite Mapping

- map each Python subsystem to its Swift or Rust replacement
- define behavior parity checklist
- keep Python implementation as reference during migration
- decide cutoff point for deleting or freezing the Python host app

## Risks

## 1. Rewrite scope is larger than expected

Risk:

- BLE, audio, accessibility, and menu bar integration together may make the rewrite much larger than a UI-only project

Mitigation:

- do the architecture spike first
- keep strict phase boundaries
- port one subsystem at a time with parity checks

## 2. macOS permissions

Risk:

- microphone / accessibility permissions often behave differently outside terminal

Mitigation:

- test from bundled app early, not only from CLI
- add explicit permission diagnostics

## 3. BLE runtime edge cases during live config edits

Risk:

- reconnect timing and in-flight recording state may become inconsistent

Mitigation:

- define reconnect-safe fields
- block dangerous changes while actively recording, or defer them

## 4. Secret storage

Risk:

- API keys in plain config files are weak

Mitigation:

- short term: support env vars and optional file storage
- medium term: move to Keychain-backed storage

## Acceptance Criteria

First usable native version is done when:

- app can launch without a terminal
- app lives in the menu bar
- user can toggle translation on/off from menu bar
- BLE service keeps working in background
- config changes are reflected on the next action or reconnect
- logs and last errors are visible from the app

## Suggested First Implementation Slice

Do this first before any packaging work:

1. architecture spike: Swift-only vs Rust+Swift
2. extract current Python runtime into service modules for reference
3. add config persistence to stabilize behavior
4. build a minimal native menu bar app with:
   - status
   - translate on/off
   - reconnect
   - quit
5. port BLE and recording flow next

This is the smallest path to user-visible value while acknowledging that the long-term direction may be a Swift or Rust rewrite rather than a Python menu bar app.

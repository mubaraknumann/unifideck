/**
 * Modals — barrel export.
 *
 * Pieces exported here :
 *  - AccountSwitchModal       (Steam account change prompt)
 *  - SteamRestartModal        (post-shortcut-write reboot prompt)
 *  - UninstallConfirmModal    (delete confirmation)
 *  - CloudSaveConflictModal   (cloud save resolution)
 *  - LanguageSelectModal      (multi-language installs: GOG, Epic)
 *  - GameVaultConnectModal    (remote server vs local vault chooser)
 *  - GameVaultLocalVaultModal (local vault folder setup)
 *  - ForceSyncModal           (force-sync artwork picker)
 *  - StorageBrowserModal      (full-screen path picker)
 *  - ToastEventListener       (event-driven toast / modal host)
 *
 * Modals receive `closeModal` from `showModal()` and return
 * JSX wrapped in `<ConfirmModal>` from `@decky/ui`. The
 * listener returns null and is mounted by RootProvider.
 */
export { AccountSwitchModal } from "./AccountSwitchModal";
export { SteamRestartModal } from "./SteamRestartModal";
export { UninstallConfirmModal } from "./UninstallConfirmModal";
export { CloudSaveConflictModal } from "./CloudSaveConflictModal";
export { LanguageSelectModal } from "./LanguageSelectModal";
export { GameVaultConnectModal } from "./GameVaultConnectModal";
export { GameVaultCredentialsModal } from "./GameVaultCredentialsModal";
export { GameVaultLocalVaultModal } from "./GameVaultLocalVaultModal";
export { ForceSyncModal } from "./ForceSyncModal";
export { ChromiumInstallModal } from "./ChromiumInstallModal";
export { PickStorageModal, pickStorageForInstall } from "./PickStorageModal";
export { StoragePathPicker } from "./StoragePathPicker";

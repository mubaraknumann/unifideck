/**
 * Steam's own app-artwork component (`<AppImage app eAssetType className>`).
 *
 * It resolves a shortcut's custom grid art exactly the way the library
 * does, so our portrait and play-bar icon show the same images Steam
 * shows everywhere else. Found by an assertion string in its source.
 */
import type { ComponentType } from "react";
import { findExportInModule, isClassComponent } from "./webpack";

/** Steam's library asset types (read at runtime, 2026-11-02). */
export const AppAssetType = {
  Portrait: 0,
  Icon: 4,
} as const;

export interface AppImageProps {
  app: object;
  eAssetType: number;
  className?: string;
}

let resolved = false;
let component: ComponentType<AppImageProps> | null = null;

export function getAppImage(): ComponentType<AppImageProps> | null {
  if (resolved) return component;
  resolved = true;
  component = findExportInModule(
    ["Either rgSources or app must be specified"],
    isClassComponent,
  ) as ComponentType<AppImageProps> | null;
  return component;
}

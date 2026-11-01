import { describe, expect, it, vi } from "vitest";

vi.mock("i18next", () => ({
  default: {
    t: (key: string) =>
      ({
        "deckTabs.gamevault": "GameVault",
        "deckTabs.epic": "Epic",
        "gameStoreSwitcher.defaultEdition": "Standard Edition",
      }[key] ?? key),
  },
}));
vi.mock("@decky/ui", () => ({ Dropdown: () => null }));
vi.mock("../shared/StoreIcon", () => ({ StoreIcon: () => null }));
vi.mock("../../lib/steam-bridge", () => ({ navigateToApp: vi.fn() }));
vi.mock("../../lib/library-filters", () => ({
  appIdsMatch: (a: number, b: number) => a === b,
}));

import type { ReactElement, ReactNode } from "react";
import { Dropdown } from "@decky/ui";
import { GameStoreSwitcher } from "./GameStoreSwitcher";
import type { GroupSibling } from "../../lib/library-filters";

function textParts(node: ReactNode): string[] {
  if (typeof node === "string" || typeof node === "number") return [String(node)];
  if (Array.isArray(node)) return node.flatMap(textParts);
  if (node && typeof node === "object" && "props" in node) {
    return textParts((node as ReactElement<{ children?: ReactNode }>).props.children);
  }
  return [];
}

function findDropdown(node: ReactNode): ReactElement | undefined {
  if (Array.isArray(node)) return node.map(findDropdown).find(Boolean);
  if (node && typeof node === "object" && "props" in node) {
    const el = node as ReactElement<{ children?: ReactNode }>;
    return el.type === Dropdown ? el : findDropdown(el.props.children);
  }
  return undefined;
}

/** The rows the switcher would list, in order, as `[appId, visible text]`. */
function listedRows(siblings: GroupSibling[]): [number, string][] {
  const rendered = GameStoreSwitcher({ appId: siblings[0].appId, siblings }) as ReactNode;
  const dropdown = findDropdown(rendered);
  const options = (dropdown!.props as { rgOptions: { data: number; label: ReactNode }[] })
    .rgOptions;
  return options.map((o) => [o.data, textParts(o.label).join(" ")]);
}

const sibling = (appId: number, extra: Partial<GroupSibling> = {}): GroupSibling => ({
  appId,
  store: "gamevault",
  title: "Game",
  ...extra,
});

describe("GameStoreSwitcher rows", () => {
  it("labels same-store copies by the store's strings and lists the better rank first", () => {
    expect(
      listedRows([
        sibling(1, { disambiguators: ["Windows", "Ghostship"], defaultRank: 1 }),
        sibling(2, { disambiguators: ["Linux", "Ghostship"], defaultRank: 0 }),
        sibling(3, { disambiguators: ["Linux", "sm64coopdx"], defaultRank: 0 }),
      ]),
    ).toEqual([
      [2, "GameVault Standard Edition Linux · Ghostship"],
      [3, "GameVault Standard Edition Linux · sm64coopdx"],
      [1, "GameVault Standard Edition Windows · Ghostship"],
    ]);
  });

  it("adds nothing when store and edition already tell the rows apart", () => {
    expect(
      listedRows([
        sibling(1, { store: "epic", disambiguators: ["Linux"] }),
        sibling(2, { disambiguators: ["Linux"] }),
      ]),
    ).toEqual([
      [1, "Epic Standard Edition"],
      [2, "GameVault Standard Edition"],
    ]);
  });

  it("shows a row's own edition and no suffix for stores that give no strings", () => {
    expect(
      listedRows([
        sibling(1, { editionLabel: "Deluxe" }),
        sibling(2, { editionLabel: "Standard" }),
      ]),
    ).toEqual([
      [1, "GameVault Deluxe"],
      [2, "GameVault Standard"],
    ]);
  });

  it("keeps identical labels when no string separates the rows", () => {
    const rows = listedRows([
      sibling(1, { disambiguators: ["Linux"] }),
      sibling(2, { disambiguators: ["Linux"] }),
    ]);
    expect(rows.map(([, text]) => text)).toEqual([
      "GameVault Standard Edition Linux",
      "GameVault Standard Edition Linux",
    ]);
  });
});

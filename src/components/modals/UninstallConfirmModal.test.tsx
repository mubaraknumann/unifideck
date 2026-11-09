/** The uninstall dialog counts Xbox PC saves that have not uploaded (I-040). */
import { describe, expect, it, vi } from "vitest";
import type { TFunction } from "i18next";

vi.mock("@decky/ui", () => ({}));
vi.mock("react-i18next", () => ({}));
vi.mock("../../api", () => ({}));

import { prefixWarningText } from "./UninstallConfirmModal";

const t = ((key: string, vars?: { count: number }) =>
  vars ? `${key}:${vars.count}` : key) as unknown as TFunction;

describe("prefixWarningText", () => {
  it("counts saves that have not uploaded", () => {
    expect(prefixWarningText(t, 2)).toBe("uninstallModal.unuploadedBody:2");
  });

  it("keeps the general warning when there are none", () => {
    expect(prefixWarningText(t, 0)).toBe("uninstallModal.warningBody");
  });
});

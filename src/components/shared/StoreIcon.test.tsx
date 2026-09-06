import { describe, it, expect } from "vitest";
import type { ReactElement } from "react";
import { StoreIcon } from "./StoreIcon";
import { GameVaultIcon } from "./GameVaultIcon";

/** Calling an FC directly returns `ReactNode`, so name the shape we assert on. */
type PathEl = ReactElement<{
  d: string;
  fillRule?: string;
  opacity?: number;
  transform?: string;
}>;
type SvgEl = ReactElement<{
  viewBox: string;
  fill: string;
  width: number | string;
  height: number | string;
  style: { color?: string };
  transform?: string;
  children: PathEl;
}>;

describe("StoreIcon", () => {
  it("renders GameVaultIcon for gamevault store", () => {
    const element = StoreIcon({
      store: "gamevault",
      size: 20,
      color: "#4ade80",
    }) as ReactElement<{ size: number; color: string }>;
    expect(element.type).toBe(GameVaultIcon);
    expect(element.props.size).toBe(20);
    expect(element.props.color).toBe("#4ade80");
  });
});

describe("GameVaultIcon", () => {
  const svg = GameVaultIcon({}) as SvgEl;
  const path = svg.props.children;

  it("uses a square viewBox so the mark cannot sit off-centre", () => {
    const [minX, minY, w, h] = svg.props.viewBox.split(" ").map(Number);
    expect(minX).toBe(0);
    expect(minY).toBe(0);
    expect(w).toBe(h);
  });

  it("carries no transform wrapper", () => {
    // The mark used to be nested in translate/scale groups with a per-path
    // mirror matrix, which is how it drifted into a corner at half size
    // without anyone noticing. Coordinates are baked in now.
    expect(svg.props.transform).toBeUndefined();
    expect(path.type).toBe("path");
    expect(path.props.transform).toBeUndefined();
  });

  it("fills the viewBox in its constrained dimension", () => {
    const nums = (path.props.d.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
    const xs = nums.filter((_, i) => i % 2 === 0);
    const ys = nums.filter((_, i) => i % 2 === 1);
    // Taller than wide, so height is the constrained dimension: it should
    // reach close to both edges of the 24-unit box, and be centred in x.
    expect(Math.min(...ys)).toBeLessThan(1.5);
    expect(Math.max(...ys)).toBeGreaterThan(22.5);
    const midX = (Math.min(...xs) + Math.max(...xs)) / 2;
    expect(Math.abs(midX - 12)).toBeLessThan(0.5);
  });

  it("is a single flat currentColor fill, not opacity layers", () => {
    expect(svg.props.fill).toBe("currentColor");
    expect(path.props.fillRule).toBe("evenodd");
    expect(path.props.opacity).toBeUndefined();
  });

  it("applies the color prop and forwards size to both axes", () => {
    const el = GameVaultIcon({ size: 18, color: "#fff" }) as SvgEl;
    expect(el.props.width).toBe(18);
    expect(el.props.height).toBe(18);
    expect(el.props.style.color).toBe("#fff");
  });
});

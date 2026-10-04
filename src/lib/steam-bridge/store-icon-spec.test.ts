/**
 * Reading store logos out of icon elements as plain SVG data.
 *
 * The elements below are built in the two shapes the real glyphs produce
 * under Steam's React: a react-icons `IconBase` element (viewBox on
 * `props.attr`, shapes as a children array) and `GameVaultIcon`'s plain
 * `<svg>` element (viewBox on props, one child). vitest's React stub does
 * not build real elements, so they are written out by hand.
 */
import { describe, expect, it } from "vitest";
import { iconSpecFromElement } from "./store-icon-spec";

const IconBase = () => null;

describe("iconSpecFromElement", () => {
  it("reads a react-icons glyph", () => {
    const element = {
      type: IconBase,
      props: {
        attr: { role: "img", viewBox: "0 0 24 24" },
        children: [{ type: "path", props: { d: "M3.5 0C2.1 0 1.6.5 1.6 1.8Z", key: 0 } }],
      },
    };

    expect(iconSpecFromElement(element)).toEqual({
      viewBox: "0 0 24 24",
      nodes: [{ tag: "path", attrs: { d: "M3.5 0C2.1 0 1.6.5 1.6 1.8Z" } }],
    });
  });

  it("reads a plain svg element and converts camelCase props", () => {
    const element = {
      type: "svg",
      props: {
        viewBox: "0 0 24 24",
        fill: "currentColor",
        children: {
          type: "path",
          props: { fillRule: "evenodd", clipRule: "evenodd", d: "M10 10Z" },
        },
      },
    };

    expect(iconSpecFromElement(element)).toEqual({
      viewBox: "0 0 24 24",
      nodes: [
        {
          tag: "path",
          attrs: { "fill-rule": "evenodd", "clip-rule": "evenodd", d: "M10 10Z" },
        },
      ],
    });
  });

  it("keeps nested groups and skips React-only props", () => {
    const element = {
      type: IconBase,
      props: {
        attr: { viewBox: "0 0 512 512" },
        children: [
          {
            type: "g",
            props: {
              transform: "translate(1 2)",
              style: { color: "red" },
              className: "x",
              children: [{ type: "circle", props: { cx: 4, cy: 4, r: 2 } }, null, false],
            },
          },
        ],
      },
    };

    expect(iconSpecFromElement(element)).toEqual({
      viewBox: "0 0 512 512",
      nodes: [
        {
          tag: "g",
          attrs: { transform: "translate(1 2)" },
          children: [{ tag: "circle", attrs: { cx: "4", cy: "4", r: "2" } }],
        },
      ],
    });
  });

  it("returns null without a viewBox or without shapes", () => {
    expect(iconSpecFromElement(null)).toBeNull();
    expect(iconSpecFromElement({ type: "svg", props: { children: [] } })).toBeNull();
    expect(
      iconSpecFromElement({ type: "svg", props: { viewBox: "0 0 1 1", children: [] } }),
    ).toBeNull();
  });
});

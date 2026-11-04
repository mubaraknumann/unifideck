/**
 * NativeCollections: "Found in these collections" at the foot of Game info.
 *
 * The same section Steam's Game info ends with, built from Steam's own
 * parts (collection store, collection tile, virtual grid, the page's scroll
 * container), exactly as Steam's tab wires them. Renders nothing when this
 * Steam build has moved one of the parts or the game is in no collection.
 */
import { FC, useId } from "react";
import { Focusable } from "@decky/ui";
import {
  getCollectionsParts,
  getNativeClasses,
  steamText,
  type CollectionsParts,
} from "../../lib/steam-bridge/native-app-details";

/** Steam's cover size for these tiles (read live 2026-11-02). */
const COVER_SIZE_MEDIUM = 2;

const Collections: FC<{ appId: number; parts: CollectionsParts }> = ({
  appId,
  parts,
}) => {
  const headerId = useId();
  const scroll = parts.useScrollContainer();
  const list = parts.listFor(appId);
  if (!list.length || !scroll) return null;
  const c = getNativeClasses().details;
  const { Tile, Grid, Cell } = parts;
  const tiles = list.map((collection) => (
    <Cell key={collection.id}>
      <Tile
        bStartFocused={false}
        collection={collection}
        eCoverSize={COVER_SIZE_MEDIUM}
        bNoFooterFilter
      />
    </Cell>
  ));
  return (
    <Focusable className={c.GameInfoCollections}>
      <div className={c.CollectionsHeaderContainer}>
        <div id={headerId} className={c.CollectionsHeader}>
          {steamText(
            "#AppDetails_GameInfo_CollectionsHeader",
            "Found in these collections",
          )}
        </div>
        <div className={c.CollectionsHeaderHR} />
      </div>
      <Grid
        aria-labelledby={headerId}
        {...parts.tileGrid}
        scaleGridItems={1}
        renderOutsideRows={2}
        paddingLeft={0} // rtl-ignore: Steam Grid prop names, both sides 0
        paddingRight={0} // rtl-ignore: Steam Grid prop names, both sides 0
        gridClassName={c.Grid}
        scrollElement={scroll.Element}
        childElements={tiles}
        name="GameInfo Collections"
      />
    </Focusable>
  );
};

export const NativeCollections: FC<{ appId: number }> = ({ appId }) => {
  const parts = getCollectionsParts();
  return parts ? <Collections appId={appId} parts={parts} /> : null;
};

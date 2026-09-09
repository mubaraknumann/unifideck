/**
 * Steam's per-device compatibility bitfield.
 *
 * `AppOverview.steam_hw_compat_category_packed` (protobuf field 73) is
 * one uint32 holding a 2-bit rating for each device. Read off the live
 * client's own getters:
 *
 *   steam_deck_compat_category    = packed & 3         // bits 0-1
 *   steam_os_compat_category      = (packed >> 4) & 3  // bits 4-5
 *   steam_machine_compat_category = (packed >> 6) & 3  // bits 6-7
 *   steam_frame_compat_category   = (packed >> 8) & 3  // bits 8-9
 *
 * We used to write bits 0-1 only. Steam's own badge and library-filter
 * components pick their field from the *running* device, so on a Steam
 * Machine every Unifideck shortcut read as Unknown — invisible to the
 * native "Verified and Playable" filter the plugin deliberately feeds,
 * and to the device-compat filter Steam applies across all library
 * views.
 *
 * The values written are Valve's own integers, passed through
 * untouched. That matters most for the SteamOS track, whose 3-value
 * enum does not mean verified/playable: routing it through the Deck
 * ladder first would store a number Steam's own filter misreads.
 */

export type CompatTrack = "deck" | "steamos" | "machine" | "frame";

/** Bit offset of each track inside the packed field. */
export const PACKED_SHIFTS: Record<CompatTrack, number> = {
  deck: 0,
  steamos: 4,
  machine: 6,
  frame: 8,
};

const MASK = 3;

/**
 * The best rating each track can carry.
 *
 * Not uniform: the Deck, Machine and Frame tracks use Valve's 4-value
 * ladder topping out at 3 (Verified), while the SteamOS track is a
 * 3-value enum whose best value is 2 (Compatible) and which never
 * emits 3. Comparing any track against a hardcoded 3 makes the SteamOS
 * branch unreachable — which silently dropped every Valve-rated native
 * Steam game out of the compatibility tab on non-Deck SteamOS hardware.
 */
export const TOP_CATEGORY: Record<CompatTrack, number> = {
  deck: 3,
  steamos: 2,
  machine: 3,
  frame: 3,
};

/** Whether `category` is the best rating `track` can express. */
export function isTopRated(category: number, track: CompatTrack): boolean {
  return category >= TOP_CATEGORY[track];
}

/** Minimal shape we read the packed field off. */
export interface PackedCompatCarrier {
  steam_hw_compat_category_packed?: number;
}

/**
 * Merge known categories into `cur`, leaving every other bit alone.
 *
 * A track with no rating (0, missing, or non-numeric) is skipped rather
 * than written: Steam may already hold a real value for a device we
 * have nothing to say about, and zeroing it would hide the game from
 * that device's filter.
 */
export function packCompat(
  cur: number,
  categories: Partial<Record<CompatTrack, number>>,
): number {
  let out = (cur || 0) >>> 0;
  for (const track of Object.keys(PACKED_SHIFTS) as CompatTrack[]) {
    const value = categories[track];
    // `Number.isFinite` rather than `typeof === "number"`: NaN is a
    // number and is not `<= 0`, so it would slip through and write 0.
    if (value === undefined || !Number.isFinite(value) || value <= 0) continue;
    const shift = PACKED_SHIFTS[track];
    out = (out & ~(MASK << shift)) | ((value & MASK) << shift);
  }
  return out >>> 0;
}

/** Read one track's category out of an overview's packed field. */
export function overviewCompatCategory(
  overview: PackedCompatCarrier | null | undefined,
  track: CompatTrack,
): number {
  const packed = overview?.steam_hw_compat_category_packed ?? 0;
  return (packed >>> PACKED_SHIFTS[track]) & MASK;
}

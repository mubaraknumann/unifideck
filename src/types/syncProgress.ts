
export interface SyncProgressCurrentGame {
    label: string;
    values: Record<string, string | number>;
}

export type SyncProgress = {
    total_games: number;
    synced_games: number;
    current_game: SyncProgressCurrentGame;
    status: string;
    progress_percent: number;
    error?: string;
    // Artwork tracking fields
    artwork_total?: number;
    artwork_synced?: number;
    current_phase?: string;
}
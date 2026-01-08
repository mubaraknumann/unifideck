/**
 * TypeScript type definitions for Download Queue API
 */

// Download status enum matching Python backend
export type DownloadStatus = 'queued' | 'downloading' | 'completed' | 'cancelled' | 'error';

// Storage location enum
export type StorageLocation = 'internal' | 'sdcard';

/**
 * Single download item from the queue
 */
export interface DownloadItem {
    id: string;                    // Unique ID, e.g., "epic:game123"
    game_id: string;               // Store-specific game identifier
    game_title: string;
    store: 'epic' | 'gog' | 'amazon';
    status: DownloadStatus;
    progress_percent: number;      // 0-100
    downloaded_bytes: number;
    total_bytes: number;
    speed_mbps: number;
    eta_seconds: number;
    error_message?: string;
    added_time: number;            // Unix timestamp
    start_time?: number;           // Unix timestamp
    end_time?: number;             // Unix timestamp
    storage_location: StorageLocation;
}

/**
 * Response from get_download_queue_info()
 */
export interface DownloadQueueInfo {
    success: boolean;
    current: DownloadItem | null;
    queued: DownloadItem[];
    finished: DownloadItem[];
    state: 'idle' | 'running';
    error?: string;
}

/**
 * Storage location option from get_storage_locations()
 */
export interface StorageLocationInfo {
    id: StorageLocation;
    label: string;                 // "Internal Storage" or "SD Card"
    path: string;                  // Actual filesystem path
    available: boolean;            // Whether this location is usable
    free_space_gb: number;         // Free space in GB
}

/**
 * Response from get_storage_locations()
 */
export interface StorageLocationsResponse {
    success: boolean;
    locations: StorageLocationInfo[];
    default: StorageLocation;
    error?: string;
}

/**
 * Response from is_game_downloading()
 */
export interface IsDownloadingResponse {
    success: boolean;
    is_downloading: boolean;
    download_info?: DownloadItem;
    error?: string;
}

/**
 * Generic success/error response
 */
export interface ApiResponse {
    success: boolean;
    error?: string;
    download_id?: string;
    position?: number;
}

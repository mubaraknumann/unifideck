export interface Game {
  id: string;
  title: string;
  store: 'steam' | 'epic' | 'gog' | 'amazon';
  coverImage?: string;
  isInstalled: boolean;
  installPath?: string;
  executable?: string;
  sizeBytes?: number;
  deckRating?: 'verified' | 'playable' | 'unsupported' | 'unknown';
}

export interface InstallProgress {
  status: 'queued' | 'downloading' | 'installing' | 'completed' | 'error';
  progress?: number;
  message?: string;
}

export interface FilterOptions {
  store: 'all' | 'steam' | 'epic' | 'gog' | 'amazon';
  sortBy: 'title' | 'playtime' | 'recent' | 'size';
  deckRating?: 'all' | 'verified' | 'playable';
}

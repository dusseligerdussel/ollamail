import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import { audioUrl, type DigestSummary, pickAudioFormat } from "@/api/digest";

/** Seconds for the skip buttons, the arrow keys and the Media Session seek actions. */
export const SKIP_SECONDS = 15;
export const playbackRates = [1, 1.25, 1.5, 1.75, 2] as const;
const RATE_STORAGE_KEY = "ollamail.digest.rate";

export interface LoadedDigest {
  id: string;
  title: string;
  /** From the server, used until the browser knows the duration. */
  durationSeconds: number | null;
}

export interface DigestPlayer {
  /** The digest in the player, also while paused. */
  loaded: LoadedDigest | null;
  playing: boolean;
  currentTime: number;
  duration: number;
  rate: number;
  /** Loads the digest if needed and toggles playback. */
  toggle: (digest: DigestSummary) => void;
  pause: () => void;
  seekBy: (seconds: number) => void;
  seekTo: (seconds: number) => void;
  setRate: (rate: number) => void;
}

const PlayerContext = createContext<DigestPlayer | null>(null);

function storedRate(): number {
  try {
    const value = Number(localStorage.getItem(RATE_STORAGE_KEY));
    return (playbackRates as readonly number[]).includes(value) ? value : 1;
  } catch {
    return 1;
  }
}

function finite(value: number, fallback = 0) {
  return Number.isFinite(value) ? value : fallback;
}

function mediaSession(): MediaSession | undefined {
  return typeof navigator !== "undefined" && "mediaSession" in navigator
    ? navigator.mediaSession
    : undefined;
}

/**
 * Owns the `<audio>` element of the digest page, so playback continues while the archive is
 * shown (stacked mobile layout). Connects it to the Media Session API: title on the lock
 * screen, play/pause and seeking from headphones, notifications and the lock screen.
 */
export function DigestPlayerProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [loaded, setLoaded] = useState<LoadedDigest | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [mediaDuration, setMediaDuration] = useState(Number.NaN);
  const [rate, setRateState] = useState(storedRate);

  const duration = finite(mediaDuration, loaded?.durationSeconds ?? 0);

  const play = useCallback(() => {
    // Rejected e.g. when the browser blocks autoplay; the state stays "paused".
    void audioRef.current?.play()?.catch(() => undefined);
  }, []);

  const pause = useCallback(() => audioRef.current?.pause(), []);

  const toggle = useCallback(
    (digest: DigestSummary) => {
      const audio = audioRef.current;
      if (!audio) return;
      if (loaded?.id !== digest.id) {
        const format = pickAudioFormat(digest.audio_formats, (type) => !!audio.canPlayType(type));
        if (!format) return;
        // Set synchronously within the click, so mobile browsers allow playback.
        audio.src = audioUrl(digest.id, format);
        audio.playbackRate = rate;
        setLoaded({ id: digest.id, title: digest.title, durationSeconds: digest.duration_seconds });
        setCurrentTime(0);
        setMediaDuration(Number.NaN);
        play();
        return;
      }
      if (audio.paused) play();
      else audio.pause();
    },
    [loaded?.id, rate, play],
  );

  const seekTo = useCallback((seconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const end = finite(audio.duration, Number.POSITIVE_INFINITY);
    audio.currentTime = Math.min(Math.max(0, seconds), end);
    setCurrentTime(audio.currentTime);
  }, []);

  const seekBy = useCallback(
    (seconds: number) => seekTo((audioRef.current?.currentTime ?? 0) + seconds),
    [seekTo],
  );

  const setRate = useCallback((value: number) => {
    setRateState(value);
    if (audioRef.current) audioRef.current.playbackRate = value;
    try {
      localStorage.setItem(RATE_STORAGE_KEY, String(value));
    } catch {
      // Not persisted (e.g. private mode); the rate still applies to this page.
    }
  }, []);

  // Lock screen / headphone controls.
  useEffect(() => {
    const session = mediaSession();
    if (!session || !loaded) return;
    if (typeof MediaMetadata !== "undefined") {
      session.metadata = new MediaMetadata({
        title: loaded.title,
        artist: "ollamail",
        album: t("digest.title"),
        artwork: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
        ],
      });
    }
    const handlers: [MediaSessionAction, MediaSessionActionHandler][] = [
      ["play", play],
      ["pause", pause],
      ["stop", pause],
      ["seekbackward", (details) => seekBy(-(details.seekOffset ?? SKIP_SECONDS))],
      ["seekforward", (details) => seekBy(details.seekOffset ?? SKIP_SECONDS)],
      ["seekto", (details) => details.seekTime !== undefined && seekTo(details.seekTime)],
    ];
    for (const [action, handler] of handlers) {
      try {
        session.setActionHandler(action, handler);
      } catch {
        // Action not supported by this browser.
      }
    }
    return () => {
      for (const [action] of handlers) {
        try {
          session.setActionHandler(action, null);
        } catch {
          // See above.
        }
      }
      session.metadata = null;
    };
  }, [loaded, t, play, pause, seekBy, seekTo]);

  useEffect(() => {
    const session = mediaSession();
    if (!session) return;
    session.playbackState = loaded ? (playing ? "playing" : "paused") : "none";
  }, [loaded, playing]);

  useEffect(() => {
    const session = mediaSession();
    if (!session?.setPositionState || !loaded || !(duration > 0)) return;
    try {
      session.setPositionState({
        duration,
        playbackRate: rate,
        position: Math.min(currentTime, duration),
      });
    } catch {
      // Inconsistent values while the media loads.
    }
  }, [loaded, duration, rate, currentTime]);

  // Stop playback when the page is left.
  useEffect(() => () => audioRef.current?.pause(), []);

  const value = useMemo<DigestPlayer>(
    () => ({
      loaded,
      playing,
      currentTime,
      duration,
      rate,
      toggle,
      pause,
      seekBy,
      seekTo,
      setRate,
    }),
    [loaded, playing, currentTime, duration, rate, toggle, pause, seekBy, seekTo, setRate],
  );

  return (
    <PlayerContext value={value}>
      {children}
      {/* biome-ignore lint/a11y/useMediaCaption: the transcript is shown next to the player */}
      <audio
        ref={audioRef}
        preload="metadata"
        className="hidden"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
        onDurationChange={(event) => setMediaDuration(event.currentTarget.duration)}
        onLoadedMetadata={(event) => {
          event.currentTarget.playbackRate = rate;
          setMediaDuration(event.currentTarget.duration);
        }}
      />
    </PlayerContext>
  );
}

export function useDigestPlayer(): DigestPlayer {
  const player = useContext(PlayerContext);
  if (!player) throw new Error("useDigestPlayer() must be used within <DigestPlayerProvider>");
  return player;
}

/** `m:ss` or `h:mm:ss`. */
export function formatDuration(totalSeconds: number) {
  const seconds = Math.max(0, Math.floor(finite(totalSeconds)));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = String(seconds % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

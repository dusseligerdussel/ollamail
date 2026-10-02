import { Pause, Play, RotateCcw, RotateCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { DigestSummary } from "@/api/digest";
import {
  formatDuration,
  playbackRates,
  SKIP_SECONDS,
  useDigestPlayer,
} from "@/components/digest/player-provider";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Slider } from "@/components/ui/slider";

export function formatRate(rate: number, locale: string) {
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(rate)}×`;
}

/** Play/pause, ±15 s, position and speed for one digest. */
export function AudioPlayer({ digest }: { digest: DigestSummary }) {
  const { t, i18n } = useTranslation();
  const player = useDigestPlayer();
  const active = player.loaded?.id === digest.id;
  const playing = active && player.playing;
  const position = active ? player.currentTime : 0;
  const duration = active ? player.duration : (digest.duration_seconds ?? 0);
  const valueText = t("digest.player.positionText", {
    position: formatDuration(position),
    duration: formatDuration(duration),
  });

  return (
    <section
      aria-label={t("digest.player.label")}
      className="flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center sm:gap-4"
    >
      <div className="flex items-center justify-center gap-1 sm:justify-start">
        <Button
          variant="ghost"
          size="icon"
          disabled={!active}
          onClick={() => player.seekBy(-SKIP_SECONDS)}
          aria-label={t("digest.player.back", { seconds: SKIP_SECONDS })}
          title={t("digest.player.back", { seconds: SKIP_SECONDS })}
        >
          <RotateCcw />
        </Button>
        <Button
          size="icon"
          className="rounded-full"
          onClick={() => player.toggle(digest)}
          aria-label={playing ? t("digest.player.pause") : t("digest.player.play")}
          title={playing ? t("digest.player.pause") : t("digest.player.play")}
        >
          {playing ? <Pause /> : <Play />}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          disabled={!active}
          onClick={() => player.seekBy(SKIP_SECONDS)}
          aria-label={t("digest.player.forward", { seconds: SKIP_SECONDS })}
          title={t("digest.player.forward", { seconds: SKIP_SECONDS })}
        >
          <RotateCw />
        </Button>
      </div>
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <span className="w-12 shrink-0 text-right text-xs text-muted-foreground tabular-nums">
          {formatDuration(position)}
        </span>
        <Slider
          className="flex-1"
          min={0}
          max={Math.max(duration, 1)}
          step={1}
          value={[Math.min(position, Math.max(duration, 1))]}
          disabled={!active}
          onValueChange={([value]) => value !== undefined && player.seekTo(value)}
          aria-label={t("digest.player.position")}
          aria-valuetext={valueText}
        />
        <span className="w-12 shrink-0 text-xs text-muted-foreground tabular-nums">
          {formatDuration(duration)}
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              className="w-16 shrink-0 text-ui tabular-nums"
              aria-label={t("digest.player.speedLabel", {
                rate: formatRate(player.rate, i18n.language),
              })}
            >
              {formatRate(player.rate, i18n.language)}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuRadioGroup
              value={String(player.rate)}
              onValueChange={(value) => player.setRate(Number(value))}
            >
              {playbackRates.map((rate) => (
                <DropdownMenuRadioItem key={rate} value={String(rate)} className="tabular-nums">
                  {formatRate(rate, i18n.language)}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </section>
  );
}

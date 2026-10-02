import { CalendarDays, CalendarPlus } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { addDays, formatDue, formatDueLong, nextWeek } from "@/lib/task-dates";
import { cn } from "@/lib/utils";

interface DueDatePickerProps {
  /** `YYYY-MM-DD` or `null`. */
  value: string | null;
  today: string;
  onChange: (value: string | null) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Done tasks show their date without the overdue colour. */
  done?: boolean;
  className?: string;
}

/** The due date of a task as a small button; opens quick choices and a date field. */
export function DueDatePicker({
  value,
  today,
  onChange,
  open,
  onOpenChange,
  done = false,
  className,
}: DueDatePickerProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const overdue = !done && value !== null && value < today;
  const label = value
    ? t("tasks.dueOn", { date: formatDueLong(value, locale) })
    : t("tasks.setDate");

  const choose = (next: string | null) => {
    onOpenChange(false);
    if (next !== value) onChange(next);
  };

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label={label}
          title={label}
          data-overdue={overdue || undefined}
          className={cn(
            "h-7 shrink-0 gap-1.5 px-2 text-xs font-normal text-muted-foreground tabular-nums",
            "data-overdue:text-destructive",
            !value &&
              "opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 group-data-active:opacity-100 data-[state=open]:opacity-100 pointer-coarse:opacity-100 max-sm:opacity-100",
            className,
          )}
        >
          {value ? (
            <>
              <CalendarDays aria-hidden="true" />
              {formatDue(value, today, locale)}
            </>
          ) : (
            <CalendarPlus aria-hidden="true" />
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-64 p-1" aria-label={t("tasks.dueDate")}>
        {open && <DueDateChoices value={value} today={today} onChoose={choose} />}
      </PopoverContent>
    </Popover>
  );
}

function DueDateChoices({
  value,
  today,
  onChoose,
}: {
  value: string | null;
  today: string;
  onChoose: (value: string | null) => void;
}) {
  const { t, i18n } = useTranslation();
  const [draft, setDraft] = useState(value ?? "");
  const weekday = (day: string) =>
    new Intl.DateTimeFormat(i18n.language, { weekday: "short", timeZone: "UTC" }).format(
      new Date(`${day}T00:00:00Z`),
    );
  const choices = [
    { key: "today", label: t("tasks.date.today"), day: today },
    { key: "tomorrow", label: t("tasks.date.tomorrow"), day: addDays(today, 1) },
    { key: "nextWeek", label: t("tasks.date.nextWeek"), day: nextWeek(today) },
  ];

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft) onChoose(draft);
  };

  return (
    <div className="flex flex-col">
      {choices.map((choice) => (
        <button
          key={choice.key}
          type="button"
          aria-pressed={choice.day === value}
          onClick={() => onChoose(choice.day)}
          className="group/choice flex items-center justify-between rounded-sm px-2 py-1.5 text-left text-ui outline-none hover:bg-accent focus-visible:bg-accent aria-pressed:font-medium"
        >
          <span>{choice.label}</span>
          <span className="text-xs text-muted-foreground group-hover/choice:text-accent-foreground group-focus-visible/choice:text-accent-foreground">
            {weekday(choice.day)}
          </span>
        </button>
      ))}
      {value && (
        <button
          type="button"
          onClick={() => onChoose(null)}
          className="rounded-sm px-2 py-1.5 text-left text-ui outline-none hover:bg-accent focus-visible:bg-accent"
        >
          {t("tasks.date.none")}
        </button>
      )}
      <form onSubmit={submit} className="mt-1 flex items-center gap-1 border-t px-1 pt-2 pb-1">
        <Input
          type="date"
          aria-label={t("tasks.dueDate")}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="h-8 flex-1 text-ui md:text-ui"
        />
        <Button type="submit" size="sm" variant="secondary" disabled={!draft}>
          {t("tasks.date.apply")}
        </Button>
      </form>
    </div>
  );
}

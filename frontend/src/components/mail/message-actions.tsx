import { useQuery } from "@tanstack/react-query";
import { Archive, Flag, FolderInput, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { type Folder, foldersQueryOptions } from "@/api/mail";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** Roles that are no move target: own buttons (trash) or server-managed folders. */
const NOT_A_TARGET = new Set(["all", "drafts", "sent", "trash"]);

function moveTargets(folders: Folder[]) {
  return folders
    .filter((folder) => !folder.role || !NOT_A_TARGET.has(folder.role))
    .sort(
      (a, b) =>
        Number(b.role === "inbox") - Number(a.role === "inbox") || a.name.localeCompare(b.name),
    );
}

interface MessageActionsProps {
  mailboxId: string;
  flagged: boolean;
  onArchive: () => void;
  onTrash: () => void;
  onMove: (folderId: string) => void;
  onToggleFlag: () => void;
  /** The move menu, controlled so the `v` shortcut can open it. */
  moveOpen: boolean;
  onMoveOpenChange: (open: boolean) => void;
}

/** Archive, move, trash and flag in the header of an opened message (#148). */
export function MessageActions({
  mailboxId,
  flagged,
  onArchive,
  onTrash,
  onMove,
  onToggleFlag,
  moveOpen,
  onMoveOpenChange,
}: MessageActionsProps) {
  const { t } = useTranslation();
  const folders = useQuery({ ...foldersQueryOptions(mailboxId), enabled: moveOpen });
  const flagLabel = t("mail.actions.flag");

  return (
    <div className="flex items-center gap-0.5">
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={onArchive}
        aria-label={t("mail.actions.archive")}
        title={t("mail.actions.archive")}
      >
        <Archive />
      </Button>
      <DropdownMenu open={moveOpen} onOpenChange={onMoveOpenChange}>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={t("mail.actions.move")}
            title={t("mail.actions.move")}
          >
            <FolderInput />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="max-h-80 w-56 overflow-y-auto">
          <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">
            {t("mail.actions.moveTo")}
          </DropdownMenuLabel>
          {folders.isPending &&
            [0, 1, 2].map((row) => <Skeleton key={row} className="mx-2 my-2 h-3" />)}
          {folders.isError && (
            <p className="px-2 py-1.5 text-ui text-muted-foreground">
              {t("mail.actions.foldersFailed")}
            </p>
          )}
          {folders.data && moveTargets(folders.data).length === 0 && (
            <p className="px-2 py-1.5 text-ui text-muted-foreground">
              {t("mail.actions.noFolders")}
            </p>
          )}
          {folders.data &&
            moveTargets(folders.data).map((folder) => (
              <DropdownMenuItem key={folder.id} onSelect={() => onMove(folder.id)}>
                <span className="truncate">
                  {folder.role === "inbox" ? t("mail.inboxFolder") : folder.name}
                </span>
              </DropdownMenuItem>
            ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={onTrash}
        aria-label={t("mail.actions.trash")}
        title={t("mail.actions.trash")}
      >
        <Trash2 />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={onToggleFlag}
        aria-label={flagLabel}
        aria-pressed={flagged}
        title={flagLabel}
      >
        <Flag className={cn(flagged && "fill-current text-brand")} />
      </Button>
    </div>
  );
}

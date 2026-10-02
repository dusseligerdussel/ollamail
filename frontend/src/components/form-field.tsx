import { type ComponentProps, useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface FormFieldProps extends Omit<ComponentProps<typeof Input>, "id"> {
  label: string;
  /** Help text below the field. */
  description?: string;
  /** Validation message; marks the field invalid. */
  error?: string;
}

/** Labelled text input with optional help text and error, wired up for screen readers. */
export function FormField({ label, description, error, ...props }: FormFieldProps) {
  const id = useId();
  const descriptionId = `${id}-description`;
  const errorId = `${id}-error`;
  const describedBy = [description && descriptionId, error && errorId].filter(Boolean).join(" ");
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id} className="text-ui">
        {label}
      </Label>
      <Input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        {...props}
      />
      {description && (
        <p id={descriptionId} className="text-xs text-muted-foreground">
          {description}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

/** Error of a whole form (e.g. wrong credentials), announced when it appears. */
export function FormError({ children }: { children: string }) {
  return (
    <p role="alert" className="text-ui text-destructive">
      {children}
    </p>
  );
}

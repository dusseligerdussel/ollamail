import { useMemo } from "react";
import { encode } from "uqr";

/**
 * QR code as inline SVG, generated in the browser (`uqr`, MIT); the value never leaves the
 * device. Always dark on white with a quiet zone, so scanners read it in the dark theme too.
 */
export function QrCode({
  value,
  label,
  size = 176,
}: {
  value: string;
  label: string;
  size?: number;
}) {
  const { path, dimension } = useMemo(() => {
    const qr = encode(value, { ecc: "M", border: 2 });
    let d = "";
    qr.data.forEach((row, y) => {
      row.forEach((dark, x) => {
        if (dark) d += `M${x} ${y}h1v1h-1z`;
      });
    });
    return { path: d, dimension: qr.size };
  }, [value]);

  return (
    <svg
      role="img"
      aria-label={label}
      width={size}
      height={size}
      viewBox={`0 0 ${dimension} ${dimension}`}
      shapeRendering="crispEdges"
      className="shrink-0 rounded-md border bg-white"
    >
      <path d={path} fill="#000" />
    </svg>
  );
}

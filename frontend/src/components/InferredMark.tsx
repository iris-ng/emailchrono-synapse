import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Sparkles } from "lucide-react";

type Place = { left: number; top?: number; bottom?: number };

const TIP_WIDTH = 360;
const CLOSE_DELAY_MS = 150;

/**
 * "inferred" pill with its own tooltip: opens on hover, keyboard focus or click
 * (click pins it open). The tooltip is rendered in a portal with fixed
 * position, so card overflow rules cannot clip it, and it does not rely on the
 * browser's `title` tooltip, which some browsers and embedded views never show.
 */
export function InferredMark({
  label = "inferred",
  heading,
  items = [],
  footer
}: {
  label?: string;
  heading: string;
  items?: { name: string; text: string }[];
  footer?: string;
}) {
  const markRef = useRef<HTMLButtonElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | null>(null);
  const overTip = useRef(false);
  const tipId = useId();
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [place, setPlace] = useState<Place | null>(null);
  const open = hover || pinned;

  function cancelClose() {
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
  }

  function show() {
    cancelClose();
    const rect = markRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.min(TIP_WIDTH, window.innerWidth - 16);
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    const roomBelow = window.innerHeight - rect.bottom;
    setPlace(
      roomBelow < 200 && rect.top > roomBelow
        ? { left, bottom: window.innerHeight - rect.top + 6 }
        : { left, top: rect.bottom + 6 }
    );
    setHover(true);
  }

  function hideSoon() {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setHover(false), CLOSE_DELAY_MS);
  }

  useEffect(() => {
    if (!open) return;
    const close = () => {
      overTip.current = false;
      setHover(false);
      setPinned(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!markRef.current?.contains(target) && !tipRef.current?.contains(target)) close();
    };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  useEffect(() => cancelClose, []);

  const stop = (event: { stopPropagation: () => void }) => event.stopPropagation();

  return (
    <>
      <button
        ref={markRef}
        type="button"
        className="inferred-mark"
        aria-expanded={open}
        aria-describedby={open ? tipId : undefined}
        onMouseEnter={show}
        onMouseLeave={hideSoon}
        onFocus={show}
        onBlur={() => {
          // Clicking into the tooltip (to copy evidence) also blurs the mark: keep it open.
          if (overTip.current) return;
          setHover(false);
          setPinned(false);
        }}
        onClick={(event) => {
          event.stopPropagation();
          show();
          setPinned((value) => !value);
        }}
        onDoubleClick={stop}
        onKeyDown={stop}
      >
        <Sparkles size={11} aria-hidden="true" /> {label}
      </button>
      {open &&
        place &&
        createPortal(
          <div
            id={tipId}
            ref={tipRef}
            role="tooltip"
            className="inferred-tip"
            style={{ left: place.left, top: place.top, bottom: place.bottom }}
            onMouseEnter={() => {
              overTip.current = true;
              cancelClose();
            }}
            onMouseLeave={() => {
              overTip.current = false;
              hideSoon();
            }}
            onMouseDown={stop}
            onClick={stop}
            onDoubleClick={stop}
          >
            <strong>{heading}</strong>
            {items.length > 0 && (
              <dl>
                {items.map((item) => (
                  <div key={item.name}>
                    <dt>{item.name}</dt>
                    <dd>{item.text || "No evidence recorded."}</dd>
                  </div>
                ))}
              </dl>
            )}
            {footer && <p>{footer}</p>}
          </div>,
          document.body
        )}
    </>
  );
}

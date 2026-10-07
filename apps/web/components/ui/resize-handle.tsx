"use client"

import * as React from "react"
import { cn } from "cn"

const STEP = 10
const LARGE_STEP = 50

type Range = { now: number; min: number; max: number }

// A vertical boundary that sets the width of the pane just before it. Drag it,
// or focus it and use the arrow keys (Shift for larger steps, Home and End for
// the limits); a double click or Enter returns the pane to the layout's own
// width. The handle only reports widths: the caller applies them, so a drag
// restyles one element per frame instead of re-rendering the pane.
function ResizeHandle({
  label,
  title,
  className,
  bounds,
  onPreview,
  onCommit,
  onReset,
}: {
  label: string
  title?: string
  className?: string
  // The widths the pane may take right now, read when a drag or a key starts.
  bounds: (handle: HTMLElement) => { min: number; max: number }
  onPreview: (width: number) => void
  onCommit: (width: number) => void
  onReset: () => void
}) {
  const ref = React.useRef<HTMLDivElement>(null)
  const drag = React.useRef<Range & { pointer: number; startX: number; start: number } | null>(null)
  const [range, setRange] = React.useState<Range | null>(null)
  const [dragging, setDragging] = React.useState(false)

  const measure = React.useCallback((handle: HTMLElement): Range => {
    const { min, max } = bounds(handle)
    const pane = handle.previousElementSibling
    return { now: pane ? pane.getBoundingClientRect().width : min, min, max: Math.max(min, max) }
  }, [bounds])

  React.useEffect(() => {
    if (ref.current) setRange(measure(ref.current))
  }, [measure])

  const finish = (event: React.PointerEvent<HTMLDivElement>) => {
    const current = drag.current
    if (!current || current.pointer !== event.pointerId) return
    drag.current = null
    setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    setRange({ now: current.now, min: current.min, max: current.max })
    // A click without movement leaves the layout's own width in place.
    if (current.now !== current.start) onCommit(current.now)
  }

  const reset = () => {
    onReset()
    requestAnimationFrame(() => { if (ref.current) setRange(measure(ref.current)) })
  }

  return (
    <div
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemax={range ? Math.round(range.max) : undefined}
      aria-valuemin={range ? Math.round(range.min) : undefined}
      aria-valuenow={range ? Math.round(range.now) : undefined}
      className={cn(
        "group/resize relative flex shrink-0 cursor-col-resize touch-none justify-center outline-none select-none",
        className
      )}
      data-dragging={dragging ? "" : undefined}
      data-slot="resize-handle"
      onDoubleClick={reset}
      onFocus={(event) => setRange(measure(event.currentTarget))}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault()
          reset()
          return
        }
        const { now, min, max } = measure(event.currentTarget)
        const step = event.shiftKey ? LARGE_STEP : STEP
        const target =
          event.key === "ArrowLeft" ? now - step
          : event.key === "ArrowRight" ? now + step
          : event.key === "Home" ? min
          : event.key === "End" ? max
          : null
        if (target === null) return
        event.preventDefault()
        const next = Math.min(Math.max(target, min), max)
        setRange({ now: next, min, max })
        onCommit(next)
      }}
      onLostPointerCapture={finish}
      onPointerCancel={finish}
      onPointerDown={(event) => {
        if (event.button !== 0 || drag.current) return
        // No text selection or focus scroll while the boundary moves.
        event.preventDefault()
        const measured = measure(event.currentTarget)
        drag.current = { ...measured, pointer: event.pointerId, startX: event.clientX, start: measured.now }
        event.currentTarget.setPointerCapture(event.pointerId)
        setDragging(true)
      }}
      onPointerMove={(event) => {
        const current = drag.current
        if (!current || current.pointer !== event.pointerId) return
        current.now = Math.min(Math.max(current.start + event.clientX - current.startX, current.min), current.max)
        onPreview(current.now)
      }}
      onPointerUp={finish}
      ref={ref}
      role="separator"
      tabIndex={0}
      title={title}
    >
      <div className="w-px bg-border transition-colors group-hover/resize:bg-ring group-focus-visible/resize:bg-ring group-data-dragging/resize:bg-ring" />
    </div>
  )
}

export { ResizeHandle }

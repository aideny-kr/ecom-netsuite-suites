"use client";

import { useId } from "react";

/** The approved abstract metal motif. Decorative motion never represents a run. */
export function MetalOrbits({ className = "" }: { className?: string }) {
  const gradient = useId().replace(/:/g, "");
  return (
    <div className={`metal-orbits ${className}`} aria-hidden="true">
      <svg viewBox="0 0 400 340" focusable="false">
        <defs><linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
          <stop stopColor="#314351" /><stop offset=".32" stopColor="#8cbcdc" />
          <stop offset=".48" stopColor="#d8edf9" /><stop offset=".55" stopColor="#7194ad" />
          <stop offset="1" stopColor="#283d4f" />
        </linearGradient></defs>
        <g transform="translate(212 173)">
          <circle r="96" stroke="currentColor" opacity=".35" fill="none" strokeWidth=".6" />
          <g className="metal-orbit-plane">
            <ellipse rx="145" ry="59" fill="none" stroke={`url(#${gradient})`} strokeWidth="5" />
            <circle className="metal-orbit-traveler" r="3" />
          </g>
          <ellipse rx="142" ry="60" transform="rotate(34)" fill="none" stroke="#719db9" strokeWidth=".9" />
          <ellipse rx="63" ry="136" transform="rotate(-18)" fill="none" stroke="#5b788e" strokeWidth=".7" />
          <g transform="rotate(34)"><circle className="metal-orbit-traveler metal-orbit-secondary" r="2.5" /></g>
          <circle r="3" fill="#d8bd9f" />
          <path d="M-173 0h20M153 0h20M0-148v15M0 133v15" stroke="currentColor" opacity=".6" strokeWidth=".7" />
        </g>
      </svg>
    </div>
  );
}

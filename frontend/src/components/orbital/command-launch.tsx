"use client";

import { useEffect, useId, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, BarChart3, MessageSquare, Pause, Play, Table2 } from "lucide-react";
import styles from "./command-launch.module.css";

const destinations = [
  { href: "/chat", title: "Ask in Chat", description: "A question. A new perspective.", icon: MessageSquare, label: "01 / THINK" },
  { href: "/transactions", title: "Explore transactions", description: "Follow the details. Find clarity.", icon: Table2, label: "02 / EXPLORE" },
  { href: "/reports", title: "Open reports", description: "Bring the bigger picture into view.", icon: BarChart3, label: "03 / UNDERSTAND" },
];

/** Decorative sculpture and ordinary route links; no data or execution claims. */
export function CommandLaunch() {
  const id = useId().replace(/:/g, "");
  const [paused, setPaused] = useState(false);
  const [reduced, setReduced] = useState(false);
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const sync = () => { setReduced(media?.matches ?? true); setHidden(document.hidden); };
    try { setPaused(localStorage.getItem("orbital-motion-paused") === "true"); } catch { /* Storage is optional. */ }
    sync();
    media?.addEventListener("change", sync);
    document.addEventListener("visibilitychange", sync);
    return () => {
      media?.removeEventListener("change", sync);
      document.removeEventListener("visibilitychange", sync);
    };
  }, []);

  return (
    <section className={styles.launch} aria-label="Launch your next task" data-motion-paused={paused || reduced || hidden}>
      <div className={styles.stage}>
        <div className={styles.copy}>
          <p className={styles.eyebrow}><span /> A SPACE FOR YOUR NEXT IDEA</p>
          <h2>Your next move,<br /><span>in focus.</span></h2>
          <p className={styles.description}>Ask a better question. Follow a detail.<br className="hidden sm:block" /> See where it takes you.</p>
          <Link href="/chat" className={styles.primary}>Start a conversation <ArrowUpRight size={17} /></Link>
        </div>
        <div className={styles.sculpture} aria-hidden="true">
          <div className={styles.halo} />
          <svg viewBox="0 0 520 430" focusable="false" className={styles.orbit}>
            <defs>
              <linearGradient id={`${id}-metal`} x1="0" y1="0" x2="1" y2="1">
                <stop stopColor="#243747" /><stop offset=".23" stopColor="#8cbbd7" /><stop offset=".43" stopColor="#effaff" />
                <stop offset=".51" stopColor="#638caa" /><stop offset=".72" stopColor="#243747" /><stop offset=".93" stopColor="#bddef2" />
              </linearGradient>
              <radialGradient id={`${id}-core`} cx=".3" cy=".2" r=".85">
                <stop stopColor="#effaff" /><stop offset=".26" stopColor="#a7cbe2" /><stop offset=".6" stopColor="#476a86" /><stop offset="1" stopColor="#142330" />
              </radialGradient>
              <linearGradient id={`${id}-rim`}><stop stopColor="#87b5d4" stopOpacity="0" /><stop offset=".5" stopColor="#d8ecfa" /><stop offset="1" stopColor="#87b5d4" stopOpacity="0" /></linearGradient>
            </defs>
            <g transform="translate(260 215)">
              <circle r="177" fill="none" stroke="currentColor" strokeWidth=".6" opacity=".2" strokeDasharray="2 9" />
              <circle r="196" fill="none" stroke="currentColor" strokeWidth=".5" opacity=".12" />
              <path d="M-212 0h12M200 0h12M0-212v12M0 200v12" stroke="currentColor" opacity=".45" />
              <g className={styles.rings}>
                <ellipse rx="166" ry="69" transform="rotate(-32)" fill="none" stroke={`url(#${id}-metal)`} strokeWidth="13" />
                <ellipse rx="166" ry="69" transform="rotate(-32)" fill="none" stroke={`url(#${id}-rim)`} strokeWidth="1" />
                <ellipse rx="82" ry="170" transform="rotate(-27)" fill="none" stroke={`url(#${id}-metal)`} strokeWidth="7" />
                <circle r="67" fill={`url(#${id}-core)`} />
                <circle r="67" fill="none" stroke="#d5eaf8" strokeOpacity=".32" strokeWidth=".7" />
                <path d="M-166 0a166 69 0 0 0 332 0" transform="rotate(-32)" fill="none" stroke={`url(#${id}-metal)`} strokeWidth="13" />
                <ellipse rx="185" ry="81" transform="rotate(29)" fill="none" stroke={`url(#${id}-rim)`} strokeWidth="1.3" />
              </g>
              <g className={styles.satellite}><circle cx="177" r="4" fill="#c8e8fc" /><circle cx="177" r="9" fill="#b4dfff" opacity=".12" /></g>
            </g>
          </svg>
          <span className={styles.coordinate}>PERSPECTIVE CHANGES EVERYTHING</span>
        </div>
        <button type="button" className={styles.motion} disabled={reduced} aria-pressed={paused || reduced} onClick={() => {
          const next = !paused; setPaused(next);
          try { localStorage.setItem("orbital-motion-paused", String(next)); } catch { /* Keep in-memory preference. */ }
        }}>
          {paused || reduced ? <Play size={12} /> : <Pause size={12} />}
          {reduced ? "Reduced motion" : paused ? "Resume motion" : "Pause motion"}
        </button>
      </div>
      <nav className={styles.destinations} aria-label="Command Center starting actions">
        {destinations.map(({ href, title, description, icon: Icon, label }) => (
          <Link key={href} href={href} className={styles.destination}>
            <span className={styles.cardTop}><span>{label}</span><Icon size={18} /></span>
            <span className={styles.cardTitle}>{title}<ArrowUpRight size={18} /></span>
            <span className={styles.cardDescription}>{description}</span>
          </Link>
        ))}
      </nav>
    </section>
  );
}

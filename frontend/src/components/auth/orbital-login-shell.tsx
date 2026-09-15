"use client";

import { useEffect, useState, type ReactNode } from "react";
import { Orbit, Pause, Play } from "lucide-react";
import { MetalOrbits } from "@/components/orbital/metal-orbits";
import styles from "./orbital-login-shell.module.css";

export function OrbitalLoginShell({ children }: { children: ReactNode }) {
  const [paused, setPaused] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => {
      setReduced(media.matches);
      setHidden(document.hidden);
    };
    try {
      setPaused(localStorage.getItem("orbital-motion-paused") === "true");
    } catch { /* Motion controls still work when storage is unavailable. */ }
    sync();
    media.addEventListener("change", sync);
    document.addEventListener("visibilitychange", sync);
    return () => {
      media.removeEventListener("change", sync);
      document.removeEventListener("visibilitychange", sync);
    };
  }, []);

  function toggleMotion() {
    const next = !paused;
    setPaused(next);
    try { localStorage.setItem("orbital-motion-paused", String(next)); } catch { /* Keep session preference. */ }
  }

  return (
    <div className={`dark ${styles.shell}`} data-motion-paused={paused || hidden || reduced}>
      <div className={styles.space} aria-hidden="true">
        <MetalOrbits className={styles.system} />
      </div>
      <header className={styles.header}>
        <div className={styles.brand}>
          <span className={styles.brandIcon}><Orbit size={23} strokeWidth={1.4} aria-hidden="true" /></span>
          <span>Suite Studio<span className={styles.brandCaption}>ORBITAL WORKSPACE</span></span>
        </div>
        <button type="button" className={styles.motion} onClick={toggleMotion} disabled={reduced}
          aria-pressed={paused || reduced} aria-label={reduced ? "Reduced motion enabled" : paused ? "Resume background animation" : "Pause background animation"}>
          {paused || reduced ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}
          <span>{reduced ? "Reduced motion" : paused ? "Motion paused" : "Pause motion"}</span>
        </button>
      </header>
      <main className={styles.main}>
        <section className={styles.intro} aria-label="Your Orbital workspace">
          <p className={styles.eyebrow}><span /> YOUR CONNECTED WORKSPACE</p>
          <h2>Your work.<br /><em>In orbit.</em></h2>
          <p className={styles.description}>Your systems, your team, your next idea.<br />One place to move work forward.</p>
        </section>
        <section className={styles.card} aria-label="Sign in to your workspace">{children}</section>
      </main>
      <footer className={styles.footer}><span>Suite Studio AI</span><span>Connected systems. Clearer possibilities.</span></footer>
    </div>
  );
}

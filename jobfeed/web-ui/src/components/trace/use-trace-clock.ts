import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";

export const SPEEDS = [10, 30, 60] as const;
export type Speed = (typeof SPEEDS)[number];

const MIN_DOMAIN = 60;
const DOMAIN_LEAD = 1.55;
const MAX_FRAME_SECONDS = 0.1;
const COMMIT_INTERVAL_MS = 100;
// A live run's open spans end at the server's read time, which is also the run's
// end, so the live clock sits just before it to keep those spans running.
const LIVE_LAG = 0.001;
const livePosition = (end: number) => Math.max(0, end - LIVE_LAG);

const targetDomain = (now: number) => Math.max(MIN_DOMAIN, now * DOMAIN_LEAD);

export interface ClockState {
  now: number;
  domain: number;
  playing: boolean;
  scrubbing: boolean;
  speed: Speed;
}

interface ClockOptions {
  end: number;
  initialNow?: number;
  autoPlay?: boolean;
  /** Pin the clock to the run's growing end instead of replaying it. */
  live?: boolean;
  onFrame?: (clock: Readonly<ClockState>) => void;
}

function sameView(a: ClockState, b: ClockState) {
  return a.now === b.now && a.playing === b.playing && a.scrubbing === b.scrubbing
    && a.speed === b.speed && Math.abs(a.domain - b.domain) < 0.5;
}

function prefersReducedMotion() {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * One requestAnimationFrame clock for a trace. Every frame it writes
 * --trace-now, --trace-domain and --trace-start onto the target element, so bar
 * motion never waits on React; the returned view updates at about 10 Hz.
 */
export function useTraceClock(target: RefObject<HTMLElement | null>, { end, initialNow = 0, autoPlay = true, live = false, onFrame }: ClockOptions) {
  const initial = (): ClockState => ({
    now: live ? livePosition(end) : initialNow, domain: targetDomain(live ? livePosition(end) : initialNow), playing: !live && autoPlay && initialNow < end, scrubbing: false, speed: 30,
  });
  const clock = useRef<ClockState>(initial());
  // React renders from this throttled copy; bars move off the CSS variables instead.
  const [view, setView] = useState<ClockState>(initial);
  const onFrameRef = useRef(onFrame);
  useLayoutEffect(() => { onFrameRef.current = onFrame; });

  const commit = useCallback(() => {
    const next = { ...clock.current };
    setView((previous) => (sameView(previous, next) ? previous : next));
  }, []);

  useLayoutEffect(() => {
    const reduced = prefersReducedMotion();
    let frameId = 0;
    let last = performance.now();
    let lastCommit = last;
    const write = () => {
      const element = target.current;
      if (!element) return;
      element.style.setProperty("--trace-now", clock.current.now.toFixed(3));
      element.style.setProperty("--trace-domain", clock.current.domain.toFixed(3));
      element.style.setProperty("--trace-start", "0");
    };
    const frame = (time: number) => {
      const state = clock.current;
      const dt = Math.min(MAX_FRAME_SECONDS, Math.max(0, (time - last) / 1000));
      last = time;
      const wasPlaying = state.playing;
      if (live) Object.assign(state, { now: livePosition(end), playing: false, scrubbing: false });
      else if (state.playing && !state.scrubbing) {
        state.now = Math.min(end, state.now + dt * state.speed);
        if (state.now >= end) state.playing = false;
      }
      if (!state.scrubbing) {
        const goal = targetDomain(state.now);
        state.domain = reduced ? goal : state.domain + (goal - state.domain) * Math.min(1, dt * 5);
      }
      write();
      if (time - lastCommit >= COMMIT_INTERVAL_MS || wasPlaying !== state.playing) {
        lastCommit = time;
        commit();
      }
      onFrameRef.current?.(state);
      frameId = requestAnimationFrame(frame);
    };
    write();
    frameId = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(frameId);
  }, [commit, end, live, target]);

  const seekFraction = useCallback((fraction: number) => {
    const state = clock.current;
    state.now = Math.max(0, Math.min(end, Math.max(0, Math.min(1, fraction)) * state.domain));
  }, [end]);

  return {
    view,
    restart: useCallback(() => {
      Object.assign(clock.current, { now: 0, playing: true, scrubbing: false });
      commit();
    }, [commit]),
    togglePlay: useCallback(() => {
      clock.current.playing = !clock.current.playing;
      commit();
    }, [commit]),
    setSpeed: useCallback((speed: Speed) => {
      clock.current.speed = speed;
      commit();
    }, [commit]),
    seekBy: useCallback((seconds: number) => {
      const state = clock.current;
      Object.assign(state, { now: Math.max(0, Math.min(end, state.now + seconds)), playing: false });
      commit();
    }, [commit, end]),
    scrubStart: useCallback((fraction: number) => {
      Object.assign(clock.current, { scrubbing: true, playing: false });
      seekFraction(fraction);
      commit();
    }, [commit, seekFraction]),
    scrubMove: seekFraction,
    scrubEnd: useCallback(() => {
      clock.current.scrubbing = false;
      commit();
    }, [commit]),
  };
}

/**
 * Where a number on this page comes from. There are only two answers, and every panel that
 * shows a figure uses one of them:
 *
 * - MEASURED: read off a real run of the Python service, named by build, machine and date.
 * - COMPUTED: worked out by this page as you use it, on a virtual clock with a seeded PRNG.
 *
 * Keeping both in one place is what stops a later panel from quietly presenting a simulated
 * duration as a measurement.
 */

export const MEASURED = {
  command: "make demo",
  target: "compose stack",
  version: "5.0.0",
  commit: "a924dcd",
  date: "2026-09-15",
  machine: "macOS 26.0.1, arm64, 10 cpus",
} as const;

/** One line naming the run the measured numbers come from. */
export const MEASURED_LABEL =
  `measured: ${MEASURED.command} on the ${MEASURED.target}, ` +
  `${MEASURED.version}, ${MEASURED.commit}, ${MEASURED.date}`;

/** One line for anything this page produces while you drive it. */
export const COMPUTED_LABEL = "computed here: virtual clock, seeded PRNG, no network";

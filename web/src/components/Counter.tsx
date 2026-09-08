import { animate, useMotionValue, useReducedMotion, useTransform, motion } from "framer-motion";
import { useEffect } from "react";

interface CounterProps {
  value: number;
  duration?: number;
  delay?: number;
  format?: (n: number) => string;
  className?: string;
}

/** Number that counts up from zero the first time it renders. */
export function Counter({ value, duration = 1.6, delay = 0, format, className }: CounterProps) {
  const reduce = useReducedMotion();
  const mv = useMotionValue(reduce ? value : 0);
  const text = useTransform(mv, (v) => (format ? format(Math.round(v)) : String(Math.round(v))));

  useEffect(() => {
    if (reduce) {
      mv.set(value);
      return;
    }
    const controls = animate(mv, value, { duration, delay, ease: [0.16, 1, 0.3, 1] });
    return () => controls.stop();
  }, [value, duration, delay, reduce, mv]);

  return <motion.span className={className}>{text}</motion.span>;
}

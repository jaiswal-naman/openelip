"use client";

import { Button as AriaButton, type ButtonProps as AriaButtonProps } from "react-aria-components";
import { type ButtonSize, buttonClass, type ButtonVariant, spinnerClass } from "./buttonStyles";

type ButtonProps = Omit<AriaButtonProps, "className" | "isPending"> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Waiting for an answer: the label stays, a spinner is added, and pressing does nothing until it arrives. */
  isPending?: boolean;
};

/**
 * A React Aria button in the design system's look.
 *
 * Pending is done here rather than with React Aria's own `isPending`, which announces through a live region that leaves
 * hidden `role="img"` nodes behind; axe reports them as missing alternative text until they expire. This does the same
 * job: the button stays focusable, ignores presses, and stops being a submit button so Enter cannot send the form twice.
 * Say what is happening in a `role="status"` line next to it, as the sign-in form does.
 */
export function Button({ variant, size, isPending = false, type, onPress, children, ...props }: ButtonProps) {
  return (
    <AriaButton
      {...props}
      type={isPending && type === "submit" ? "button" : type}
      onPress={isPending ? undefined : onPress}
      aria-disabled={isPending || undefined}
      className={buttonClass(variant, size)}
    >
      {(state) => (
        <>
          {typeof children === "function" ? children(state) : children}
          {isPending && <span className={spinnerClass} aria-hidden="true" />}
        </>
      )}
    </AriaButton>
  );
}

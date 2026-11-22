"use client";

import { Button as AriaButton, type ButtonProps as AriaButtonProps } from "react-aria-components";
import { type ButtonSize, buttonClass, type ButtonVariant, spinnerClass } from "./buttonStyles";

type ButtonProps = Omit<AriaButtonProps, "className"> & { variant?: ButtonVariant; size?: ButtonSize };

/** A React Aria button in the design system's look. `isPending` keeps the label and adds a spinner. */
export function Button({ variant, size, children, ...props }: ButtonProps) {
  return (
    <AriaButton {...props} className={buttonClass(variant, size)}>
      {(state) => (
        <>
          {typeof children === "function" ? children(state) : children}
          {state.isPending && <span className={spinnerClass} aria-hidden="true" />}
        </>
      )}
    </AriaButton>
  );
}

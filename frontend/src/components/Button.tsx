import type { ButtonHTMLAttributes, PropsWithChildren } from "react";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  compact?: boolean;
}

export function Button({
  children,
  variant = "secondary",
  compact = false,
  className = "",
  ...props
}: PropsWithChildren<ButtonProps>) {
  return (
    <button
      className={`button button--${variant}${compact ? " button--compact" : ""} ${className}`.trim()}
      {...props}
    >
      {children}
    </button>
  );
}

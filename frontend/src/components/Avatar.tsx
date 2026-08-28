import type { UserSummary } from "../types/crm";

interface AvatarProps {
  user: UserSummary;
  size?: "sm" | "md" | "lg";
}

export function Avatar({ user, size = "md" }: AvatarProps) {
  return (
    <span className={`avatar avatar--${user.tone} avatar--${size}`} title={user.name} aria-label={user.name}>
      {user.initials}
    </span>
  );
}

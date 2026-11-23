"use client";

import balloon from "@/components/ui/balloon.module.css";
import { IMPORTANCE_LABELS } from "@/lib/labels";
import styles from "./detail.module.css";
import type { Requirement } from "./types";

const GROUPS = ["required", "preferred"] as const;

/** The number each requirement carries on the sheet, in the order the list shows them: required first, then preferred. */
export function requirementNumbers(requirements: Requirement[]): Map<number, number> {
  const shown = GROUPS.flatMap((importance) => requirements.filter((requirement) => requirement.importance === importance));
  return new Map(shown.map((requirement, index) => [requirement.ordinal, index + 1]));
}

/**
 * A job's requirements, grouped as the posting states them: Required, then Preferred. Each is a toggle that selects the
 * passage it quotes (FRONTEND_UI 4.2); a requirement whose citation no longer resolves is shown as withheld and cannot be
 * selected, because there is nothing to highlight and the page never guesses a nearby sentence.
 */
export function RequirementList({
  requirements,
  resolved,
  selected,
  onSelect,
}: {
  requirements: Requirement[];
  /** The ordinals whose text is found at their span in the posting. */
  resolved: ReadonlySet<number>;
  selected: number | null;
  /** The ordinal pressed, or `null` when the selected one is pressed again. */
  onSelect: (ordinal: number | null) => void;
}) {
  const numbers = requirementNumbers(requirements);
  return (
    <div className={styles.groups}>
      {GROUPS.map((importance) => {
        const items = requirements.filter((requirement) => requirement.importance === importance);
        if (items.length === 0) return null;
        const headingId = `requirements-${importance}`;
        return (
          <div key={importance}>
            <h3 id={headingId} className={`t-section ${styles.groupTitle}`}>
              {IMPORTANCE_LABELS[importance]} <span className="muted">({items.length})</span>
            </h3>
            <ul className={styles.items} aria-labelledby={headingId}>
              {items.map((requirement) => (
                <li key={requirement.id}>
                  <RequirementItem
                    requirement={requirement}
                    number={numbers.get(requirement.ordinal) ?? 0}
                    resolves={resolved.has(requirement.ordinal)}
                    pressed={selected === requirement.ordinal}
                    onPress={() => onSelect(selected === requirement.ordinal ? null : requirement.ordinal)}
                  />
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

function RequirementItem({
  requirement,
  number,
  resolves,
  pressed,
  onPress,
}: {
  requirement: Requirement;
  number: number;
  resolves: boolean;
  pressed: boolean;
  onPress: () => void;
}) {
  // The importance is a word in a badge, so it is not shown by colour alone; the requirement's own words follow, as text.
  const words = (
    <>
      <span className={`${balloon.balloon} ${styles.itemNo}`} aria-hidden="true">
        {number}
      </span>
      <span className={styles.itemText}>
        <span className={`t-label ${styles.badge} ${styles[requirement.importance]}`}>{IMPORTANCE_LABELS[requirement.importance]}</span>
        <span className={styles.words}>{requirement.text}</span>
      </span>
    </>
  );

  if (!resolves) {
    return (
      <div className={`${styles.item} ${styles.withheld}`}>
        {words}
        <span className={`t-small ${styles.reason}`}>Withheld: citation did not resolve</span>
      </div>
    );
  }
  return (
    <button type="button" className={styles.item} aria-pressed={pressed} onClick={onPress}>
      {words}
    </button>
  );
}

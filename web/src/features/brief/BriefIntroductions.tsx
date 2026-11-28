import { LinkButton } from "@/components/ui/LinkButton";
import type { BriefData } from "@/features/matches/queries";
import styles from "@/features/introductions/introductions.module.css";

type Item = Extract<BriefData["decisions"][number], { kind: "introduction" }>;

/** The introduction items of a brief, as lines: who asked, about what, until when, and the link that opens the consent sheet. */
export function introductionItems(decisions: BriefData["decisions"]): Item[] {
  return decisions.filter((decision): decision is Item => decision.kind === "introduction");
}

const day = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

export function BriefIntroductions({ items }: { items: Item[] }) {
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="brief-introductions" className={styles.section}>
      <h2 id="brief-introductions" className="t-section">
        Introductions in this brief
      </h2>
      <ul className={styles.rows}>
        {items.map((item) => (
          <li key={item.introduction_id} className={styles.row}>
            <span className={styles.words}>
              {item.organization_name} asks about {item.role_title}. Answer by {day(item.expires_at)}.
            </span>
            <LinkButton href={`/app/today?decision=${item.introduction_id}`} variant="primary" size="sm">
              Review and accept
            </LinkButton>
          </li>
        ))}
      </ul>
    </section>
  );
}

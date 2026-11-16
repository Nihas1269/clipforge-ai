import { Container } from "@/components/container";
import { PRODUCT_DESCRIPTION, PRODUCT_NAME, PRODUCT_TAGLINE } from "@/lib/site";

const PLANNED_CAPABILITIES = [
  {
    title: "Upload",
    description: "Bring in long-form recordings such as podcasts, talks, and streams.",
  },
  {
    title: "Transcribe",
    description: "Generate a transcript and subtitles from the audio track.",
  },
  {
    title: "Find clips",
    description: "Surface the most engaging moments as short clip candidates.",
  },
  {
    title: "Reframe",
    description: "Crop horizontal footage into vertical formats for mobile platforms.",
  },
  {
    title: "Caption",
    description: "Style captions on each clip to match your channel.",
  },
  {
    title: "Export",
    description: "Preview finished clips and export them for publishing.",
  },
] as const;

export default function HomePage() {
  return (
    <Container className="py-16 sm:py-24">
      <section aria-labelledby="hero-heading" className="max-w-3xl">
        <p className="text-sm font-medium uppercase tracking-widest text-accent">
          Under development
        </p>
        <h1
          id="hero-heading"
          className="mt-3 text-4xl font-semibold tracking-tight text-fg sm:text-5xl"
        >
          {PRODUCT_NAME}
        </h1>
        <p className="mt-4 text-xl text-fg">{PRODUCT_TAGLINE}</p>
        <p className="mt-4 text-base leading-7 text-fg-muted">{PRODUCT_DESCRIPTION}</p>
      </section>

      <section aria-labelledby="capabilities-heading" className="mt-16">
        <h2 id="capabilities-heading" className="text-lg font-semibold text-fg">
          Planned workflow
        </h2>
        <ol className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {PLANNED_CAPABILITIES.map((capability, index) => (
            <li
              key={capability.title}
              className="rounded-lg border border-border bg-surface p-5"
            >
              <p className="font-mono text-xs text-fg-muted">
                {String(index + 1).padStart(2, "0")}
              </p>
              <h3 className="mt-2 text-base font-semibold text-fg">{capability.title}</h3>
              <p className="mt-1 text-sm leading-6 text-fg-muted">{capability.description}</p>
            </li>
          ))}
        </ol>
      </section>
    </Container>
  );
}

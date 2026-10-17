# AgentHive deck

Slides on how AgentHive works and where the time and token savings come
from. Markdown in, HTML/PDF/PPTX out — [Marp](https://marp.app/), so the
deck diffs like anything else in the repo.

```bash
npm install -g @marp-team/marp-cli

make html     # fast browser preview; presenter mode with P
make pdf      # print-ready
make pptx     # for anyone who needs it in Office
make all      # diagrams + all three
```

`make diagrams` re-renders the PlantUML the deck embeds. Those sources
live in [`../diagrams/`](../diagrams/) rather than being copied in here,
so each picture has one source and the deck cannot drift from the README.

It expects a PlantUML jar at `~/tools/plantuml-snapshot.jar`; override
with `make diagrams PLANTUML=/path/to/plantuml.jar`.

## Notes for editing

- `themes/agenthive.css` is built from the palette the repo's hero SVG
  already uses, so a slide and the README read as one thing. Flat on
  purpose: no gradients or shadows, which export badly to PDF.
- `_class: lead` is the dark opening/closing slide, `_class: stat` is a
  single large number, `_class: caveat` is for qualifying a claim rather
  than making one.
- The token figures are a worked example on a five-node neighbourhood,
  not a benchmark. The deck says so on the slide. If you quote a
  measured number, quote it from `/metrics` on a real instance.

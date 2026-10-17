# Marp deck build, same targets as the rest of polarpoint-io's decks.
#
#   make all        diagrams + html + pdf + pptx
#   make diagrams   render the .puml sources this deck embeds
#   make html       fast browser preview (presenter mode with P)
#   make pdf        print-ready
#   make pptx       for anyone who needs it in Office
#
# Requires:
#   npm install -g @marp-team/marp-cli
#   PlantUML jar at $(PLANTUML), override on the command line if yours
#   lives elsewhere: make diagrams PLANTUML=/path/to/plantuml.jar

MARP     ?= marp
PLANTUML ?= $(HOME)/tools/plantuml-snapshot.jar
DECK     := deck.md
OUT      := build
# The deck embeds diagrams from the repo's own docs/diagrams, so there is
# one source for each picture rather than a copy that drifts.
DIAGRAMS := ../diagrams

.PHONY: all diagrams html pdf pptx clean

all: diagrams html pdf pptx

diagrams:
	java -jar $(PLANTUML) -tsvg $(DIAGRAMS)/*.puml

$(OUT):
	mkdir -p $(OUT)

html: $(OUT)
	$(MARP) --no-stdin --theme-set themes/ --html $(DECK) -o $(OUT)/agenthive.html

pdf: $(OUT)
	$(MARP) --no-stdin --theme-set themes/ --pdf --allow-local-files $(DECK) -o $(OUT)/agenthive.pdf

pptx: $(OUT)
	$(MARP) --no-stdin --theme-set themes/ --pptx --allow-local-files $(DECK) -o $(OUT)/agenthive.pptx

clean:
	rm -rf $(OUT)

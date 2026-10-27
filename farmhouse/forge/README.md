# Forge in the cluster image

The batch simulator on the Simulator page runs on **Forge**, the Java rules engine. The first cluster builds left it out by mistake ("Python-only"
was read as "no Java"), so the page answered `Forge JAR not found`. It is now built into the image (reference copy of the Dockerfile stage:
`infrastructure/apps/commander-lab/image/Dockerfile` in the farmhouse-os repo).

What the image build does:

1. **Stage 1** (`maven:3.9-eclipse-temurin-17`): fetch the official `Card-Forge/forge` at commit `edb28158f6d8` (the commit `KoalaTrapLord/Forge-Repo`
   is on; that fork is 0 commits ahead of upstream, so this is the code the lab's Java was written against; newer Forge may have changed APIs), then
   `mvn install -pl forge-gui-desktop -am -DskipTests -Drevision=2.0.12-SNAPSHOT`, retried up to three times (Maven Central downloads fail now and then).
2. **The lab's Java tool** (`LabCli`, from this repository's `pom.xml`) is built against it. Forge's installed poms keep `${revision}` as their parent
   version, which Maven cannot resolve for a consumer even with `-Drevision`; the placeholder is rewritten in the installed `.pom` files first.
3. **Runtime**: the full `openjdk-17-jre` (not `-headless`) plus `xvfb`, the two jars, and Forge's `res/` data without `adventure`, `music`, `sound`, `quest` and `skins`
   (about 190 MB the headless simulator never reads). The environment the app reads: `FORGE_JAR`, `FORGE_DIR`, `LAB_JAR`, `FORGE_DECKS_DIR`, `PRECON_DIR`, `JAVA_HOME`.
4. `FORGE_MAX_THREADS` (default 1) clamps the batch's thread count: every Forge game is its own JVM with up to a 4 GB heap, and the cluster has two small nodes.
5. CI starts the app, begins a batch through the app's own `/api/lab/start` and requires a Commander game with turns played and a readable `/api/lab/result` before the image is published.

## Things that only showed up by running it in the cluster (2026-10-27)
Each of these made a batch fail while every individual piece looked installed. Together they are why the CI test starts a batch through the app's own API.
- **Forge needs a display, even for `sim`.** `GuiDesktop`'s static initialiser asks AWT for the default screen device; the headless JRE throws `HeadlessException`, and
  `forge.view.Main` then exits 1 with no output at all (the lab tool records every game as an instant draw: "Forge produced NO output"). Fix: the full `openjdk-17-jre`,
  `xvfb`, `DISPLAY=:99`, and the entrypoint starts `Xvfb :99`.
- **Three folders, one deck.** The app writes saved decks to `USER_DECKS_DIR`, the lab's Java tool resolves deck files through `PRECON_DECKS_DIR`, and Forge finds a deck BY
  NAME in its own commander folder ("Could not load deck"). All three are the Forge folder (`FORGE_DECKS_DIR`), which the entrypoint seeds with the 163 bundled precons.
- **Writable folders.** The tool writes `results/` beside Forge's data and Forge a debug log beside its jar; both folders are owned by uid 10001 in the image.
- **Result path.** The app asked for `results/ml-decision-<id>.json` (relative) but ran the tool from Forge's folder, so the file landed there and `/api/lab/result` answered
  "Result file not found". The path is made absolute (patch `forge-results-abs`) and `/app/results` is linked to `/state/results`.
- `docker cp` cannot write into a tmpfs and Docker mounts `/tmp` `noexec` (it stops SQLite's native library loading): test-harness traps, not cluster problems.

Speed: about 25 seconds per game, one at a time (the project's own figure is ~0.04 simulations/s single-threaded). For many games with real rules, the
phase-rs engine (`farmhouse/phase/`) is far faster. Forge is GPL-3.0: private use, do not redistribute the built image.

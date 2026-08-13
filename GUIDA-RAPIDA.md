# GUIDA RAPIDA — salvare le foto da una vecchia fotocamera

Otto passi, in ordine. Non serve sapere niente di computer.

Il programma si chiama **RetroCam Rescue**: copia le foto dalla fotocamera al computer. **Non cancella
niente da solo.** Cancella solo se glielo chiedi tu, alla fine, e solo dopo aver riletto e controllato
una per una le foto copiate.

## 1. Scarica il programma

Apri il browser (Chrome, Edge, Safari) e vai qui:

`https://github.com/gabrielepetteno/canon-powershot-s30-rescue/releases`

Più in basso, sotto la scritta **Assets** (in inglese vuol dire "file da scaricare"), c'è un elenco.
Ne serve **uno solo**:

- **Windows** → `RetroCam-Rescue-Windows.exe`
- **Mac con chip Apple** (M1, M2, M3, M4) → `RetroCam-Rescue-macOS-AppleSilicon.zip`
- **Mac più vecchio, con processore Intel** → `RetroCam-Rescue-macOS-Intel.zip`

Non sai che Mac hai? Menu Apple in alto a sinistra → **Informazioni su questo Mac**: accanto a «Chip»
leggerai **Apple** oppure **Intel**. Sbagliare non fa danni: il Mac dice solo che non riesce ad
aprirlo, e tu torni a prendere l'altro. Clicca sul nome del file: finisce nella cartella **Download**.

## 2. Aprilo la prima volta

**Su Windows:** doppio clic sul file `.exe`. Non c'è nessuna installazione: quel file _è_ il programma.
Apparirà una finestra blu, «Windows ha protetto il PC» → clicca **Ulteriori informazioni** →
**Esegui comunque**.

**Su Mac:** doppio clic sul file scaricato: compare l'applicazione **RetroCam Rescue**. Doppio clic
anche su quella: **non si aprirà**, ed è previsto. Compare un riquadro che dice che il Mac non può
verificare l'applicazione, con i pulsanti **Sposta nel Cestino** e **Fine**. Non spostarla nel
Cestino: clicca **Fine**. Poi:

1. menu Apple → **Impostazioni di Sistema** → **Privacy e sicurezza**;
2. scorri in fondo, alla sezione **Sicurezza**: c'è una riga che dice che «RetroCam Rescue» è stato
   bloccato → clicca **Apri comunque**, e conferma con la password del Mac o il Touch ID;
3. all'ultimo riquadro clicca **Apri**.

Non è un virus: il programma è gratuito e non ha il certificato a pagamento di Apple, quindi il Mac
non sa **chi** l'ha fatto. Tutto questo vale **solo la prima volta**.

## 3. Collega la fotocamera

Se hai un lettore di schede lascia perdere il cavo: togli la scheda dalla fotocamera, infilala nel
lettore, infila il lettore nel computer. È la strada migliore. Vai al passo 4. Altrimenti, con il cavo:

1. Metti batterie cariche, o l'alimentatore: se la fotocamera si spegne a metà, la copia si ferma.
2. Accendila in **modalità riproduzione** — quella per rivedere le foto, di solito il simbolo ▶
   (triangolino). **Non** in modalità scatto.
3. Collega il cavo USB **direttamente a una presa del computer**, non a un hub. Un _hub_ è quella
   scatoletta con più prese USB, o le prese sul monitor o sulla tastiera: lì spesso non funziona.
4. Su Mac, se si aprono da sole le app **Foto** o **Acquisizione Immagine**, chiudile: tengono
   occupata la fotocamera.

## 4. Premi «Cerca la fotocamera»

Nella finestra del programma, riquadro **2. Fotocamera**, premi **Cerca la fotocamera**.

**Su Mac** comparirà un riquadro che chiede se il programma può accedere ai file su «un volume
rimovibile»: è la tua scheda. Clicca **Consenti**. Se rispondi di no, il programma non vedrà più la
scheda.

Per qualche secondo leggerai «Ricerca della fotocamera in corso...», poi comparirà una riga così:

`Canon card (NO NAME) · porta /Volumes/NO NAME · tramite Memory card or USB drive · 128 file`

oppure, con il cavo, così:

`Canon PowerShot S30 · porta usb:001,004 · tramite gphoto2 (vintage / proprietary protocol) · 128 file`

Le parole in mezzo restano in inglese e non ti servono: guarda il nome all'inizio e **l'ultimo numero**,
che è quante foto ci sono. Se invece leggi «Nessuna fotocamera trovata», vai a «Se qualcosa non va».

**Solo su Mac e solo con il cavo:** se nel riquadro **1. Ambiente** c'è scritto che **gphoto2** è
«non installato», premi **Installa** lì di fianco e aspetta qualche minuto (è il pezzo che sa parlare
con le fotocamere vecchie). Se il programma risponde che manca anche «Homebrew», fermati: la strada
semplice è il lettore di schede.

## 5. Scegli dove salvare

Riquadro **3. Destinazione**. Il programma propone già una cartella dentro **Download**, con il nome
della fotocamera e la data di oggi:

`Download/PowerShot_S30_2026-08-13`

Va benissimo così, non toccare niente. Se ne vuoi un'altra premi **Sfoglia...** e scegli: la cartella
viene creata da sola quando parte la copia. Una sola regola: la destinazione deve stare **sul
computer**, mai sulla scheda della fotocamera — se no non esisterebbe una seconda copia.

## 6. Premi «Scarica tutto»

Riquadro **4. Download**, pulsante **Scarica tutto**. Su Mac, la prima volta, il computer chiede il
permesso di usare la cartella **Download**: clicca **Consenti**. Poi vedrai una barra che avanza e
una riga che conta i file: `1 / 128 · IMG_0001.JPG · 47%`.

**Ci vogliono minuti, non secondi.** Il cavo di queste fotocamere è lentissimo: per un centinaio di
foto conta anche 10-20 minuti (con il lettore di schede, meno di un minuto). Nel frattempo non
scollegare il cavo, non spegnere la fotocamera, non chiudere la finestra. Vai a farti un caffè. Alla
fine leggerai «Download completato» e, nel riquadro **5**, `128 di 128 scaricati e verificati`.

## 7. CONTROLLA le foto

Questo passo non si salta.

1. Apri la cartella di destinazione: **Download** → la cartella con il nome della fotocamera.
2. Guarda quanti file ci sono: il numero deve corrispondere.
3. **Apri qualche foto con un doppio clic.** Una all'inizio, una in mezzo, una alla fine. Devi vederle
   davvero grandi sullo schermo: l'anteprima piccola non basta.

Se le foto si aprono e si vedono bene, il salvataggio è riuscito. Adesso fanne una **seconda copia**:
chiavetta USB, disco esterno o cloud. Un posto solo non basta.

## 8. Solo ora, se vuoi, cancella dalla fotocamera

Non sei obbligato: se la scheda non ti serve per altro, lasciala com'è, è una copia in più. Se ti serve
spazio, riquadro **5. Dopo il download** → **Cancella dalla fotocamera**. Il programma chiede conferma
(due volte, se le foto sono 25 o più) e cancella soltanto i file che ha copiato e riletto uno per uno;
gli altri li lascia dove sono.

---

## Se qualcosa non va

- **«Nessuna fotocamera trovata»** — è accesa? è in riproduzione (▶)? il cavo è in una presa del
  computer e non in un hub? Spegni, riaccendi, premi di nuovo **Cerca la fotocamera**. Su Mac, se la
  scheda è nel lettore ma non compare: **Impostazioni di Sistema** → **Privacy e sicurezza** →
  **File e cartelle** → **RetroCam Rescue** → attiva **Volumi rimovibili**.
- **Il Mac non lo fa aprire** — rifai il passo 2.
- **Si ferma a metà, o la fotocamera si spegne** — batterie scariche o cavo che balla: cambia le
  batterie, ricollega e riparti da capo. I file già copiati restano, e niente è stato cancellato.
- **Windows non vede la fotocamera con il cavo** — è previsto, non è colpa tua: Windows non sa parlare
  con le fotocamere di quell'epoca. Serve il lettore di schede (vedi in fondo).

Detto con franchezza: la versione per Windows e quella per Mac Intel vengono costruite e provate in
automatico, ma nessuno le ha ancora usate su un computer vero. La strada provata sul serio, con foto
vere, è il Mac con chip Apple — e, su qualunque computer, il lettore di schede.

---

> ## LA REGOLA PIÙ IMPORTANTE
>
> **Non formattare la scheda e non cancellare niente finché non hai verificato che le foto si aprono e
> non ne hai una seconda copia.**
>
> Le foto cancellate da una scheda non tornano più indietro. Le foto lasciate sulla scheda non danno
> fastidio a nessuno.

---

_Nota per chi sta aiutando:_ la strada più affidabile non è il cavo, è un **lettore USB per schede
CompactFlash, 10-15 €**: niente driver, niente protocolli del 2001, nessun rischio che le batterie
muoiano a metà, e da 20 a 50 volte più veloce. Con il lettore il programma usa il percorso «scheda di
memoria», che non dipende da nessun driver ed è identico su Windows, Mac e Linux.

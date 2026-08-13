# Canon PowerShot S30 — recupera le tue foto (RetroCam Rescue)

**Tira fuori le fotografie da una Canon PowerShot S30, o da un'altra fotocamera
digitale del 1999–2002, quando Windows 11 o il Mac non vedono la fotocamera per
niente.**

[English](README.md) | Italiano

![Licenza: MIT](https://img.shields.io/badge/licenza-MIT-blue)
![Test: 387](https://img.shields.io/badge/test-387%20superati-brightgreen)
![Piattaforme: Windows, macOS, Linux](https://img.shields.io/badge/sistemi-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

Colleghi la fotocamera al computer e non succede niente. Non si apre nessuna
finestra. Non compare nessun disco. La fotocamera non è rotta e la scheda di
memoria non è rotta: sono i computer di oggi che hanno smesso di capire il modo
in cui parlano le fotocamere di quell'epoca.

RetroCam Rescue è un piccolo programma gratuito che le tue foto te le tira fuori
lo stesso. Copia ogni fotografia sul tuo computer, rilegge ogni copia per essere
sicuro che sia arrivata tutta intera, e **solo dopo** ti propone di svuotare la
fotocamera.

Funziona allo stesso modo con tutte le fotocamere che hanno lo stesso problema
della S30: Canon PowerShot **S40**, **S100 / S110 / S200** (le prime Digital
IXUS), **G1**, **G2**, **Pro90 IS**, **A10**, **A20** — e i modelli equivalenti
di Nikon, Olympus, Kodak, Fujifilm e Casio degli stessi anni.

Tutto avviene sul tuo computer. Niente viene caricato da nessuna parte. Non serve
nessun account, non serve registrarsi, non costa niente.

> **Non sei una persona tecnica e vuoi solo salvare le foto?**
> Leggi (o stampa) la **[GUIDA RAPIDA](GUIDA-RAPIDA.md)** — otto passi, nessun
> termine tecnico, due facciate di un foglio.

---

## Indice

- [Fa al caso tuo?](#fa-al-caso-tuo)
- [Come farlo funzionare](#come-farlo-funzionare)
  - [Windows, passo per passo](#windows-passo-per-passo)
  - [Mac, passo per passo](#mac-passo-per-passo)
  - [Perché il Mac dice che lo sviluppatore non è verificato](#perché-il-mac-dice-che-lo-sviluppatore-non-è-verificato)
- [Come si usa](#come-si-usa)
- [Prima di cancellare qualcosa](#prima-di-cancellare-qualcosa)
- [Se non funziona](#se-non-funziona)
- [Quale strada scegliere](#quale-strada-scegliere)
- [È sicuro?](#è-sicuro)
- [Stato del progetto](#stato-del-progetto)
- [Per chi sviluppa](#per-chi-sviluppa)

---

## Fa al caso tuo?

- Hai una vecchia fotocamera digitale, o la sua scheda di memoria, con dentro
  delle foto.
- La colleghi al computer e **non succede niente**: nessun disco nuovo, nessuna
  finestra, niente in Foto, in Acquisizione Immagine o in Esplora file.
- Vuoi quelle fotografie al sicuro sul tuo computer.

Se la tua fotocamera **invece** compare già come un disco quando la colleghi,
questo programma funziona lo stesso, ed è comunque il modo più tranquillo di fare
il lavoro: copia tutto, ricontrolla ogni file dopo averlo copiato, non
sovrascrive mai una foto, e si rifiuta di cancellare qualunque cosa non abbia
verificato.

> ### Prima che tu ti metta a cercare un driver
>
> Se cerchi su internet _"driver Canon PowerShot S30 Windows 11"_, troverai
> pagine che ti offrono esattamente quello, con un bel pulsante verde
> **Download**.
>
> **Non installarli.** Quel driver non esiste. Gli ultimi programmi ufficiali
> Canon per queste fotocamere erano per Windows XP e per i vecchi Mac, e sono
> stati ritirati da molti anni. Nessuno ne ha scritti di nuovi. I siti che
> sostengono il contrario sono quasi sempre abbonamenti a finti "aggiornatori di
> driver", pacchetti pieni di pubblicità, oppure semplicemente virus.
>
> Regola pratica: **se una pagina ti chiede di disattivare l'antivirus per far
> funzionare una fotocamera del 2001, chiudi quella pagina.**

---

## Come farlo funzionare

**Non** devi installare Python, non devi aprire il Terminale e non devi capire
nemmeno una parola della sezione per sviluppatori qui in fondo. Ti servono due
cose: scaricare un file, e farci doppio clic.

**Si comincia da qui:**
[**Pagina delle release — scarica il programma**](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/releases/latest)

"Release" vuol dire "versione pubblicata": è la pagina dove stanno i file già
pronti da scaricare. Lì dentro c'è una sezione chiamata **"Assets"** (a volte
tradotta "Risorse"). Se ti sembra un elenco chiuso, clicca sulla parola _Assets_
per aprirlo. Poi scegli il tuo file:

| Il tuo computer                        | Il file da scaricare                     |
| -------------------------------------- | ---------------------------------------- |
| Windows 10 o Windows 11                | `RetroCam-Rescue-Windows.exe`            |
| Mac con chip Apple (M1 / M2 / M3 / M4) | `RetroCam-Rescue-macOS-AppleSilicon.zip` |
| Mac con processore Intel               | `RetroCam-Rescue-macOS-Intel.zip`        |

I programmi sono tre, e i nomi non cambiano mai da una versione all'altra. Nel
nome **non** c'è nessun numero di versione: il file chiamato
`RetroCam-Rescue-Windows.exe` che trovi su quella pagina è sempre il più
recente per Windows. (C'è anche un quarto file, `SHA256SUMS.txt`: è un
foglietto di testo per i tecnici. Ignoralo.)

**Prendere il file sbagliato per il Mac non è pericoloso.** Il Mac dirà
semplicemente che non riesce ad aprirlo. Torni indietro e prendi l'altro.

> **Se hai un Mac con processore Intel, leggi qui.** Il file per Intel viene
> costruito in automatico, e un controllo automatico verifica che sia davvero un
> programma per Intel e che si avvii. Ma nessuno ha ancora salvato foto vere con
> quel file su un vero Mac Intel. Ci aspettiamo che funzioni. Non l'abbiamo
> visto funzionare. Se le foto sono importanti e hai il minimo dubbio, usa la
> strada della **scheda di memoria** spiegata in
> [Quale strada scegliere](#quale-strada-scegliere): non richiede di installare
> niente ed è la più affidabile su qualunque computer.

### Windows, passo per passo

1. Apri la [pagina delle release](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/releases/latest)
   e clicca su **`RetroCam-Rescue-Windows.exe`**. Si scarica come qualunque
   altro file, di solito nella cartella **Download**.
2. Il browser potrebbe avvisarti che questo tipo di file "può danneggiare il
   computer" e chiederti conferma. Scegli **"Mantieni"**. (In Edge e in Chrome
   a volte devi prima cliccare i tre puntini accanto al file, e poi **"Mantieni
   comunque"**.) Quel messaggio compare per ogni programma scaricato da internet,
   non solo per questo.
3. **Fai doppio clic sul file scaricato.** Non c'è niente da installare: il
   programma _è_ quel singolo file. Puoi lasciarlo in Download oppure
   trascinarlo sul Desktop, come preferisci.
4. Comparirà una finestra blu con scritto **"Windows ha protetto il PC"**. È
   normale — la spiegazione è più sotto: il programma non è firmato con un
   certificato a pagamento. Clicca sulla scritta piccola **"Ulteriori
   informazioni"**, e poi sul pulsante **"Esegui comunque"** che compare.
5. Si apre la finestra di **RetroCam Rescue**. Continua con
   [Come si usa](#come-si-usa).

**Un avviso onesto, per chi è su Windows.** Nessuno ha ancora usato questo
programma su un vero PC Windows con una vera vecchia fotocamera attaccata. I
controlli automatici confermano che la versione per Windows si costruisce e si
avvia, e la strada della scheda di memoria usa lo stesso codice che è stato
provato a fondo altrove — ma le parti che su Windows parlano con la fotocamera
non hanno mai incontrato hardware vero. Se le tue foto sono insostituibili e sei
su Windows, usa **un lettore di schede di memoria** (vedi
[Quale strada scegliere](#quale-strada-scegliere)): è la strada meglio provata su
qualunque sistema. E se invece provi lo stesso con la fotocamera attaccata,
raccontaci com'è andata: quella segnalazione vale davvero tanto.

### Mac, passo per passo

1. **Prima di tutto, guarda che Mac hai.** Clicca il **menu Apple** in alto a
   sinistra dello schermo, poi **"Informazioni su questo Mac"**.
   - Se leggi una riga tipo **"Chip: Apple M1"** (oppure M2, M3, M4), hai un Mac
     con chip Apple. Bene: vai avanti.
   - Se leggi **"Processore: ... Intel ..."**, hai un Mac Intel: prendi il file
     per Intel, dopo aver letto la nota qui sopra.
2. Apri la [pagina delle release](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/releases/latest)
   e clicca su **`RetroCam-Rescue-macOS-AppleSilicon.zip`** (oppure
   **`RetroCam-Rescue-macOS-Intel.zip`** se hai un Mac Intel). Finisce nella tua
   cartella **Download**.
3. **Fai doppio clic sul file .zip scaricato.** Si apre da solo e accanto compare
   un'applicazione chiamata **RetroCam Rescue**. Se ti piace tenere le cose in
   ordine, trascinala nella cartella **Applicazioni** — ma funziona da qualunque
   posto.
4. **Fai doppio clic sull'applicazione.** **Non** si aprirà, ed è previsto.
   Compare un riquadro che dice che il Mac non è riuscito a verificare che
   l'applicazione non contenga malware, e ti offre solo **"Sposta nel Cestino"**
   e **"Fine"**. Non spostarla nel Cestino: clicca **"Fine"**.
   ([Perché lo dice?](#perché-il-mac-dice-che-lo-sviluppatore-non-è-verificato))
5. Menu Apple (in alto a sinistra) → **"Impostazioni di Sistema"** → nella
   colonna di sinistra, **"Privacy e sicurezza"**.
6. Scorri fino alla sezione **"Sicurezza"**: adesso c'è una riga nuova che dice
   che **"RetroCam Rescue"** è stato bloccato. Clicca **"Apri comunque"** lì
   accanto e conferma con la password del Mac o con il Touch ID.
7. Compare un ultimo riquadro con il pulsante **"Apri"**: cliccalo. (Se non
   compare, fai di nuovo doppio clic sull'applicazione.) Si apre la finestra di
   **RetroCam Rescue** e **questo giro non lo rifarai mai più**. Continua con
   [Come si usa](#come-si-usa).

> Molte guide su internet dicono di fare **clic destro → "Apri"**. Su un Mac con
> macOS 14 o precedente funziona ancora e ti risparmia i passi 5 e 6 (se il mouse
> non ha il tasto destro: tieni premuto **Control** e clicca, oppure clicca con
> **due dita** sul trackpad). Apple l'ha tolto da macOS 15, quindi su un Mac
> recente il clic destro ti rimostra solo lo stesso riquadro: usa i passi qui
> sopra.

**Il Mac ti chiederà il permesso altre due volte, più avanti.** È il Mac a
chiedertelo, non il programma, e sono richieste normali:

- la prima volta che cerchi la fotocamera, un riquadro chiede se RetroCam Rescue
  può accedere ai file su **un volume rimovibile**: è la tua scheda di memoria.
  Clicca **"Consenti"**. Se rispondi di no, la scheda diventa invisibile al
  programma e ti ritrovi con "Nessuna fotocamera trovata" senza altra
  spiegazione.
- quando parte la copia, un riquadro chiede della tua cartella **Download**,
  perché è lì che vanno le foto. Clicca **"Consenti"**.

Se hai cliccato il pulsante sbagliato puoi cambiare idea: **"Impostazioni di
Sistema"** → **"Privacy e sicurezza"** → **"File e cartelle"** → **"RetroCam
Rescue"**.

**Per leggere una scheda di memoria non serve nient'altro.** Se invece vuoi
raggiungere **la fotocamera vera e propria** attraverso il cavo, su Mac serve un
altro programmino gratuito che si chiama `gphoto2`. Il riquadro **"1. Ambiente"**
dentro l'applicazione può installartelo da solo — ma solo se hai già Homebrew
(uno strumento che le persone pratiche usano per installare programmi sul Mac).
Se non ce l'hai, il programma te lo dice e **non** te lo installa di nascosto: è
una scelta voluta, perché installare Homebrew vuol dire eseguire uno script preso
da internet, cioè esattamente il genere di cosa che questa pagina ti consiglia di
non fare a scatola chiusa. In quel caso la risposta più semplice, di gran lunga,
è un lettore di schede: vedi [Quale strada scegliere](#quale-strada-scegliere).

### Perché il Mac dice che lo sviluppatore non è verificato

Questa è la parte che spaventa le persone, quindi ecco la spiegazione in parole
semplici.

Il Mac controlla se un programma è stato firmato con un certificato **comprato da
Apple**. Quel certificato si paga ogni anno, e questo programma è gratuito e non
commerciale: quindi non ce l'ha. Il Mac perciò non riesce a stabilire **chi** ha
fatto l'applicazione, e te lo dice: a seconda della versione di macOS leggerai
_"impossibile verificare lo sviluppatore"_ oppure _"Apple non ha potuto
verificare che non contenga malware"_.

> **Non è un avviso antivirus. Non è un virus.**
> Il Mac non ti sta dicendo che il programma è pericoloso: ti sta dicendo che non
> sa a chi dare la colpa nel caso lo fosse. Lo stesso identico messaggio compare
> per moltissimi programmi gratuiti e conosciutissimi.

Ed è esattamente il punto in cui la maggior parte delle persone si arrende e
rinuncia alle proprie fotografie. Sarebbe un peccato: sono due clic.

Se resti comunque in dubbio, ecco cosa puoi fare, dal più facile al più
impegnativo:

- **Usa la strada della scheda di memoria.** Non richiede di scaricare nessun
  programma da internet: bastano il lettore di schede e il gestore di file che
  hai già.
- **Fatti dare un'occhiata da qualcuno di pratico.** Tutto il codice sorgente è
  pubblico su questa pagina e chiunque può leggerlo, e ogni versione pubblicata
  ha accanto un file `SHA256SUMS.txt`, con cui una persona esperta può verificare
  che il file che hai scaricato sia esattamente quello che è stato costruito.

Il messaggio di Windows ("Windows ha protetto il PC") è la stessa identica cosa,
detta con altre parole e per lo stesso motivo.

---

## Come si usa

La finestra è una sola colonna, con cinque riquadri numerati dall'alto verso il
basso. Falli in ordine. Qualunque cosa il programma stia facendo, la scrive nel
riquadro **"Registro"** in fondo, che puoi salvare in un file con il pulsante
**"Salva il registro..."**.

Se il tuo computer è in italiano, il programma è in italiano: i nomi dei pulsanti
qui sotto sono esattamente quelli che vedrai sullo schermo.

**1. Ambiente.** Il riquadro in alto elenca cosa può usare questo computer, riga
per riga, con accanto la scritta **"disponibile"** oppure **"non installato"**, e
un pulsante **"Installa"** dove il programma può darti una mano. **Qui non devi
fare niente**: una scheda di memoria dentro un lettore non ha bisogno di nulla di
tutto questo. Serve soltanto se vuoi collegare la fotocamera con il cavo. Se più
avanti una fotocamera compare con sotto la scritta _"never tested on real
hardware"_ (in inglese anche se il resto è in italiano: vuol dire "mai provato
su hardware reale"), credi a quell'etichetta: è lì apposta.

**2. Fotocamera.** Collega la fotocamera con il suo cavo USB e **accendila**,
oppure metti la sua scheda di memoria dentro un lettore. Poi premi **"Cerca la
fotocamera"**. Per qualche secondo leggerai _"Ricerca della fotocamera in
corso..."_, e poi, accanto alla scritta **"Dispositivo:"**, comparirà una riga.
Con la scheda dentro un lettore è fatta così:

```
Canon card (NO NAME) · porta /Volumes/NO NAME · tramite Memory card or USB drive · 214 file
```

e con la fotocamera attaccata al cavo, così:

```
Canon PowerShot S30 · porta usb:001,004 · tramite gphoto2 (vintage / proprietary protocol) · 214 file
```

Le parole in mezzo (che restano in inglese) sono il programma che ti dice _come_
c'è arrivato; a te interessano il nome all'inizio e il numero di file alla fine.
Tutte e due queste righe vogliono dire che ha funzionato. Se il programma trova
più di un dispositivo, scegli quello giusto dall'elenco a tendina accanto al
pulsante. Se non trova niente, vai a [Se non funziona](#se-non-funziona).

**3. Destinazione.** È la cartella in cui verranno copiate le foto. È già
compilata con una proposta sensata dentro la tua cartella **Download**, con il
nome della fotocamera e la data di oggi, per esempio
`Download/PowerShot_S30_2026-08-13`. Puoi lasciarla esattamente com'è. Se ne
vuoi un'altra, premi **"Sfoglia..."** e scegli: la cartella viene creata quando
parte il download, non prima.

Una sola regola: la destinazione deve stare **su questo computer**, mai sulla
scheda della fotocamera. Se ci provi il programma si ferma e te lo spiega —
copiare le foto sulla stessa scheda da cui vengono non lascerebbe nessuna seconda
copia, e a quel punto non avresti salvato niente.

**4. Download.** Premi il pulsante grande **"Scarica tutto"**. La barra avanza e
la riga sotto dice quale file sta copiando, così:
`12 / 214 · IMG_0012.JPG · 47%`. C'è anche un pulsante **"Annulla"**: premerlo è
sempre sicuro — le foto già copiate restano copiate, e per colpa di
un'operazione annullata non viene mai cancellato niente dalla fotocamera.

Qui ci vuole pazienza. Attraverso il cavo della fotocamera è davvero lento: ne
parliamo in [Se non funziona](#se-non-funziona).

**5. Dopo il download.** Una riga in grassetto ti dice com'è andata, per esempio
_"214 di 214 scaricati e verificati"_. Sotto c'è il pulsante **"Cancella dalla
fotocamera"**.

**Perché il pulsante di cancellazione è grigio e non si preme.** Resta spento
finché _ogni singolo file_ non è stato copiato sul tuo computer, riletto e
controllato: un pulsante capace di cancellare una foto che in realtà non hai
ancora sarebbe la cosa più pericolosa dell'intero programma. Grigio vuol dire
"non ancora dimostrato", e la riga grigia sotto il pulsante ti dice cosa manca:
non hai ancora scaricato niente, oppure qualche file non ha superato i controlli,
oppure questo tipo di collegamento non può proprio cancellare (scheda protetta,
o collegamento in sola lettura). Se sulla scheda ci sono foto davvero rovinate,
quelle non supereranno mai i controlli: il pulsante resterà grigio e gli
originali resteranno dove sono. È voluto.

Quando invece lo premi, il programma ti chiede conferma con una finestra
intitolata **"Cancellare dalla fotocamera?"** — e se le foto in gioco sono 25 o
più, te la chiede una seconda volta, con la finestra **"Ultima conferma"**.

---

## Prima di cancellare qualcosa

> ## ⚠️ Le foto cancellate dalla fotocamera non tornano indietro.
>
> Non finiscono nel Cestino. Non c'è nessun "annulla", né sulla scheda né in
> questo programma. Se la copia sul computer non è quella che credevi, quella
> fotografia è persa e basta.

### Il consiglio più onesto: non cancellare niente

Non sei obbligato a cancellare. Puoi copiare tutto sul computer, chiudere il
programma e lasciare la scheda esattamente com'è. Una vecchia scheda di memoria
vale pochi euro; una fotografia di vent'anni fa non ha prezzo. Se lo spazio sulla
scheda non ti serve davvero, **la scelta migliore è lasciar perdere**: metti la
scheda in un cassetto ed è una copia in più, che non dà fastidio a nessuno.

### Se invece vuoi liberare la scheda, prima fai queste quattro cose

1. **Apri le foto e guardale davvero.** Vai nella cartella di destinazione e apri
   diverse fotografie: la prima, l'ultima, e qualcuna in mezzo. Guardale a
   schermo intero, non le miniature — una miniatura può comparire anche per un
   file danneggiato. Il programma controlla ogni file a fondo, ma i tuoi occhi
   sono l'ultima verifica, e non costano niente.
2. **Contale.** Il riassunto dice "214 di 214". Quel numero somiglia al numero di
   fotografie che ti aspettavi di trovare?
3. **Fai una seconda copia da un'altra parte**, prima di svuotare la fotocamera.
   Un'altra cartella sullo stesso computer non è un backup; un disco esterno, un
   secondo computer o una chiavetta USB sì. Le fotografie che esistono in un
   posto solo sono a un incidente di distanza dal non esistere più.
4. **E solo allora, se vuoi, cancella.** Una scheda ferma in un cassetto da
   vent'anni potrebbe non darti una seconda occasione.

### Se sospetti che la scheda sia rovinata

Se il computer la riconosce a fatica, se compaiono e scompaiono dei file, se ci
sono errori di lettura, **fermati**. Non insistere a leggerla e rileggerla: ogni
tentativo in più su una scheda che sta morendo può essere l'ultimo. Lasciala
stare e portala da chi fa recupero dati.

---

## Se non funziona

Nella tabella, la colonna di sinistra è **quello che vedi sullo schermo**. Cerca
la riga che somiglia alla tua situazione.

| Cosa vedi                                                                                        | Cosa vuol dire                                                                                                                                                                                                                                          | Cosa fare                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **"Nessuna fotocamera trovata"** — la fotocamera è attaccata e accesa                            | Quasi sicuramente non è rotto niente: è il tuo computer che non ha mai riconosciuto quella fotocamera, quindi il programma non ha nessuno con cui parlare.                                                                                              | Controlla che sia **accesa** (molte di queste fotocamere, da spente, non si fanno proprio vedere dal computer), possibilmente in modalità riproduzione ▶. Attacca il cavo **direttamente al computer**, non a un hub né alle prese del monitor. Cerca nel menù della fotocamera una voce _USB_ o _collegamento PC_. Su Mac installa gphoto2 dal riquadro 1; su Windows usa un lettore di schede.                                                                                         |
| **"Nessuna fotocamera trovata"** — la scheda è nel lettore                                       | Su Mac la causa più probabile è che al riquadro dei permessi sia stato risposto "Non consentire". Altrimenti: o la scheda non è stata montata dal computer, o le foto non stanno nella cartella `DCIM`, quella standard che creano tutte le fotocamere. | Su Mac: **"Impostazioni di Sistema"** → **"Privacy e sicurezza"** → **"File e cartelle"** → **"RetroCam Rescue"**, e attiva **"Volumi rimovibili"**. Altrimenti controlla che la scheda compaia nel Finder o in Esplora file. Se compare ma il programma non trova niente, con ogni probabilità in passato le foto sono state spostate fuori da `DCIM`: in quel caso copia le cartelle a mano, con il Finder o con Esplora file. Non si perde niente, la scheda è un disco normalissimo. |
| **"Un altro programma sta occupando la fotocamera"**                                             | Un altro programma del computer ha preso la fotocamera per primo e non la molla. È l'inconveniente più comune quando si usa il cavo, e non è un difetto della tua fotocamera.                                                                           | Su Mac chiudi del tutto **Foto** e **Acquisizione Immagine**, scollega la fotocamera, riattaccala e premi di nuovo **"Cerca la fotocamera"**. Il programma prova già da solo a liberarla prima di ogni tentativo, quindi spesso al secondo colpo funziona. Su Linux chiudi ogni finestra del gestore file che mostra la fotocamera.                                                                                                                                                      |
| **Il trasferimento è lentissimo** — minuti per poche foto                                        | Non c'è niente che non va. Quella presa USB sulla fotocamera è del 2001, e il modo in cui la fotocamera consegna i file aggiunge altro ritardo. Un lettore di schede fa lo stesso lavoro da 20 a 50 volte più in fretta.                                | Lascialo lavorare: non è bloccato, la riga cambia a ogni file. Una scheda da 128 MB può richiedere 5–10 minuti, una da 1 GB più di un'ora. Non staccare niente. Se è insopportabile, usa un lettore di schede: la stessa scheda si copia in pochi secondi.                                                                                                                                                                                                                               |
| **La fotocamera si spegne durante il trasferimento**                                             | Batterie vecchie, oppure lo spegnimento automatico della fotocamera.                                                                                                                                                                                    | Metti batterie nuove, o l'alimentatore se ce l'hai. Poi riparti semplicemente **sulla stessa cartella**: le foto già copiate vengono ricontrollate invece che riscaricate, quindi il recupero riprende da dove si era fermato.                                                                                                                                                                                                                                                           |
| **Sei su Windows e la fotocamera è precedente al 2003**                                          | Windows non sa più parlare con queste fotocamere: i vecchi driver erano a 32 bit e Windows 10 e 11 non li caricano. È un limite di Windows, non di questo programma.                                                                                    | Usa un **lettore di schede**: trasforma un problema difficile in una cosa da cinque minuti. (Esiste anche una scorciatoia tecnica: sta in [Per chi sviluppa](#per-chi-sviluppa).)                                                                                                                                                                                                                                                                                                        |
| **Il pulsante "Cancella dalla fotocamera" resta grigio**                                         | Non tutti i file sono stati copiati _e_ verificati, oppure questo collegamento non può cancellare per niente. Il pulsante sta facendo il suo mestiere.                                                                                                  | Leggi la riga grigia sotto il pulsante e il riassunto sopra: dicono cosa manca. Rilancia il download per riprovare i file falliti. I file che non passano mai i controlli sono davvero rovinati sulla scheda: lasciali lì.                                                                                                                                                                                                                                                               |
| **Alcune foto risultano danneggiate**                                                            | Probabilmente lo sono davvero. Una scheda rimasta ferma dal 2004 può avere zone rovinate, e i trasferimenti interrotti vent'anni fa possono aver lasciato file scritti a metà.                                                                          | Vengono comunque copiate sul tuo computer: semplicemente non vengono proposte per la cancellazione. Provale ad aprire (a volte si vede mezza immagine) e prova un programma di riparazione JPEG sulle copie. **Non cancellarle dalla scheda.**                                                                                                                                                                                                                                           |
| **L'applicazione Mac non si apre**, o dice che "non può essere verificata" o che "è danneggiata" | L'applicazione non è firmata con un certificato Apple a pagamento. Non è rovinata.                                                                                                                                                                      | Segui [Mac, passo per passo](#mac-passo-per-passo): **"Impostazioni di Sistema" → "Privacy e sicurezza" → "Apri comunque"**. Non cliccare **"Sposta nel Cestino"**.                                                                                                                                                                                                                                                                                                                      |
| **Windows: "Windows ha protetto il PC"**                                                         | È l'avviso di SmartScreen: il programma è nuovo e non firmato. Non è un virus trovato.                                                                                                                                                                  | **"Ulteriori informazioni" → "Esegui comunque"**. Se vuoi la certezza di cosa hai scaricato, confronta il file con i codici del `SHA256SUMS.txt` della release.                                                                                                                                                                                                                                                                                                                          |
| **Windows: il file scaricato sparisce da solo**                                                  | L'antivirus l'ha messo in quarantena: è un falso allarme tipico dei programmi impacchettati in un file unico.                                                                                                                                           | Ripristinalo dalla quarantena del tuo antivirus. Se preferisci non forzare la mano all'antivirus — ed è una scelta legittima — usa un **lettore di schede**, che non richiede di installare niente.                                                                                                                                                                                                                                                                                      |
| **Non mi ha aiutato niente di tutto questo**                                                     | —                                                                                                                                                                                                                                                       | **Compra un lettore di schede CompactFlash.** Vedi qui sotto.                                                                                                                                                                                                                                                                                                                                                                                                                            |

Se sei ancora bloccato, apri una segnalazione su
[GitHub](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/issues)
scrivendo che computer hai, che fotocamera stai usando, e **cosa hai visto sullo
schermo**, parola per parola. Se il programma si è aperto, allega anche il
registro (riquadro **"Registro"** → **"Salva il registro..."**): contiene i nomi
delle tue cartelle e dei tuoi file, quindi dagli un'occhiata e togli quello che
preferisci non pubblicare. Anche una segnalazione che racconta un fallimento è un
contributo utile, non un disturbo.

### La soluzione più efficace in assoluto

**Compra un lettore di schede CompactFlash: 10–15 €, in qualunque negozio di
elettronica o online.**

Togli la scheda di memoria dalla fotocamera, infilala nel lettore, infila il
lettore nel computer, e salta completamente la fotocamera.

Funziona perché la _scheda_ non ha mai avuto niente di esotico. Le fotocamere di
quegli anni scrivevano su normalissime schede CompactFlash, in un formato che
ogni computer costruito da allora in poi sa leggere. È soltanto il modo in cui la
_fotocamera_ parla via USB a essere diventato obsoleto.

- **Non può fallire per colpa di un driver.** Non c'è nessun driver, nessun
  protocollo, nessuna collaborazione richiesta a una fotocamera di vent'anni.
- **È da 20 a 50 volte più veloce.** Secondi, invece di un'ora.
- **Le batterie della fotocamera non possono morire a metà**, perché la
  fotocamera non è nemmeno coinvolta.

Prima di comprarlo, guarda che tipo di scheda usa la tua fotocamera: quasi tutti
questi modelli usano **CompactFlash di Tipo I**, qualcuno usa SmartMedia. Di
solito è scritto dentro lo sportellino della scheda, o sul manuale. Se le foto ti
stanno a cuore, questa è semplicemente la cosa da fare.

---

## Quale strada scegliere

Il programma non ti chiede mai di scegliere: prova da solo tutte e tre le strade
per raggiungere le tue foto, e ti mostra cosa ha trovato. Questa tabella serve
solo perché tu sappia cosa stai guardando.

| Strada                               | Che cos'è                                                                                                                        | Cosa ti serve                                                                          | Dove funziona                   | Può cancellare, dopo?                                 |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------- | ----------------------------------------------------- |
| **Scheda di memoria o disco USB**    | La scheda tolta dalla fotocamera e infilata in un lettore. **La strada consigliata.**                                            | Un lettore di schede (10–15 €). Nessun programma da installare.                        | Windows, Mac, Linux             | Sì, a meno che la scheda sia bloccata o protetta      |
| **gphoto2** (fotocamera con il cavo) | Parla direttamente con la vecchia fotocamera nella sua lingua. È l'**unico** modo di raggiungere una Canon pre-2003 con il cavo. | Il programma gratuito `gphoto2`: su Mac il riquadro 1 può installarlo, se hai Homebrew | Mac e Linux. **Non su Windows** | Di solito sì; qualche file protetto può rifiutarsi    |
| **Fotocamere Windows (WIA)**         | Usa il supporto per fotocamere già incluso in Windows, che però funziona solo per le fotocamere dal 2003 in poi.                 | Niente da installare: è già dentro il programma scaricato                              | Solo Windows                    | A volte, spesso no. **Mai provato su hardware reale** |

L'ordine in cui le prova è una scelta di sicurezza, non un dettaglio. La scheda
di memoria viene per prima perché non può fallire per colpa di un driver.
gphoto2 viene per seconda perché sa raggiungere la fotocamera vera e propria. La
strada Windows è l'ultima, perché vede solo le fotocamere che Windows già capisce
— e una PowerShot del 2001 non è tra quelle.

---

## È sicuro?

**Sì, per come è costruito. Ed ecco esattamente cosa vuol dire.**

- **Legge soltanto, finché non sei tu a dire il contrario.** Cercare la
  fotocamera, elencare le foto e copiarle non tocca mai una foto sulla scheda.
  (L'unica piccola eccezione è la prova di scrittura descritta qui sotto.)
- **Non formatta e non svuota mai una scheda.** Un pulsante del genere non
  esiste. La cancellazione avviene sempre file per file, una fotografia alla
  volta chiamata per nome, mai "tutto quello che assomiglia a...".
- **Non può cancellare una foto senza aver dimostrato di averla salvata.** Prima
  che un solo file venga cancellato, quel preciso file deve essere stato copiato
  sul tuo computer, riletto, confrontato con la dimensione dichiarata dalla
  fotocamera, controllato nella sua struttura interna di immagine, e ritrovato
  ancora al suo posto un istante prima della cancellazione. Se anche una sola di
  queste cose è incerta, la risposta è no.
- **Non sovrascrive mai niente.** Se due foto finirebbero con lo stesso nome —
  cosa che succede spesso, perché queste fotocamere ricominciano a numerare da
  capo — la seconda diventa `118CANON_IMG_0001.JPG` invece di prendere il posto
  della prima.
- **Un file copiato a metà non prende mai il nome definitivo.** Se il
  trasferimento si interrompe, non ti ritrovi mai con un file che sembra completo
  e non lo è.
- **È al 100% offline.** Nessun account, nessuna registrazione, nessun invio,
  nessun cloud, nessuna statistica, nessuna segnalazione automatica, nessun
  controllo aggiornamenti. Le tue fotografie non escono mai dal tuo computer. Il
  programma apre una connessione a internet in un caso soltanto: se sei **tu** a
  premere un pulsante **"Installa"** nel riquadro 1, ed è il gestore di programmi
  già presente sul tuo computer a scaricare quel pacchetto. Se non lo premi mai,
  il programma non tocca mai la rete.

**L'unica eccezione, detta chiaramente.** Quando usi una scheda dentro un
lettore, il programma deve capire se quella scheda accetta di essere scritta:
altrimenti non può sapere se il pulsante **"Cancella dalla fotocamera"**
funzionerebbe. Lo fa creando un file vuoto, da zero byte, dentro la cartella
`DCIM` della scheda — o, se la cartella `DCIM` non c'è, nella prima pagina della
scheda — e cancellandolo immediatamente dopo. Succede appena la scheda viene
trovata, cioè quando premi **"Cerca la fotocamera"**, non più tardi; una volta
sola per scheda, ed è l'unica scrittura che questo programma esegue al di fuori
di una cancellazione chiesta da te. Su queste
schede un file vuoto non occupa spazio reale. Se anche quella singola scrittura
ti sembra una di troppo, per una scheda che ti preoccupa, non usare il programma
direttamente sulla scheda: fai fare a un tecnico una copia integrale della scheda
e recupera le foto da quella.

**Una cosa che nessun programma può controllare:** il tuo sistema operativo può
scrivere sulla scheda per conto suo nell'istante in cui la colleghi — il Mac crea
dei file nascosti di indice, Windows crea una cartella nascosta — e succede
ancora prima che questo programma sia in esecuzione. Le schede SD hanno un
interruttorino laterale che lo impedisce; le CompactFlash no. È un motivo in più
per portare da un tecnico una scheda davvero fragile.

---

## Stato del progetto

Versione 0.1.0. Riassunto onesto, senza pubblicità.

**Cosa è provato e pronto da usare:**

- **La strada della scheda di memoria, su ogni sistema.** È il percorso meglio
  dimostrato dell'intero progetto. I suoi controlli automatici girano su cartelle
  vere, con file veri, su un disco vero: qui non c'è niente di finto.
- **macOS**, compresa l'applicazione da scaricare: è stata costruita, aperta e
  usata davvero.
- **Una vera Canon PowerShot S30**, collegata con il suo cavo tramite gphoto2 su
  macOS, dall'inizio alla fine e con fotografie vere: trovata, elencata,
  scaricata, verificata, e svuotata solo dopo la verifica. Compresi i due casi
  scomodi — due foto con lo stesso nome di file, e un file danneggiato che il
  blocco di sicurezza si è correttamente rifiutato di dichiarare salvo.
- **387 controlli automatici**, tutti superati, in circa 11 secondi, su Python
  3.9 e su Python recente, rieseguiti in automatico prima che venga pubblicato
  qualunque file scaricabile. Coprono il blocco della cancellazione, il motore di
  copia, tutte e tre le strade, e un controllo permanente per ogni difetto mai
  trovato in revisione. Anche i controlli stessi sono stati messi alla prova,
  introducendo di proposito 86 guasti nel codice per vedere se se ne
  accorgevano: li hanno colti tutti e 86.

**Cosa non è provato — per favore non dare per scontato il contrario:**

- **La strada Windows non è mai stata eseguita su hardware Windows vero.** Mai,
  neanche una volta. È scritta seguendo la documentazione di Microsoft, e provata
  soltanto contro un'imitazione di Windows, su un Mac. È costruita per fermarsi
  in modo visibile invece che sbagliare in silenzio, e ogni fotocamera che trova
  è etichettata _"never tested on real hardware"_ ("mai provato su hardware
  reale") sotto il suo stesso nome, nella finestra dove si preme Cancella — ma
  se le tue foto sono insostituibili e sei su Windows, usa un
  lettore di schede. **Cerchiamo aiuto:** una segnalazione da un vero PC Windows,
  anche una che racconta un fallimento, è il contributo più utile che questo
  progetto possa ricevere.
- **I Mac Intel.** A ogni versione viene costruita in automatico anche la
  versione per Intel, e i controlli automatici verificano che sia davvero un
  binario Intel e che si avvii. Ma nessuno ha ancora salvato foto vere con quella
  versione su un vero Mac Intel: la prova completa sul campo descritta qui sopra
  è stata fatta su un Mac con chip Apple.
- **Quasi tutta la finestra del programma** non è coperta da controlli
  automatici. Lo sono i due comportamenti che potrebbero costarti una
  fotografia: il blocco del pulsante di cancellazione, e l'azzeramento del
  risultato precedente quando cambi fotocamera.
- **Nessun controllo automatico ha mai parlato con una fotocamera vera.** I test
  della strada con il cavo usano un sostituto che imita le risposte dello
  strumento vero, e una Canon del 2001 è molto più disordinata di qualunque
  imitazione. L'unica cosa che dimostra che funziona è la prova su hardware vero
  descritta qui sopra.

---

## Per chi sviluppa

Tutto quello che sta sopra questa riga è per chi vuole solo le proprie foto.
Questa parte no.

Il pacchetto Python si chiama `retrocam` e il nome dell'applicazione resta
"RetroCam Rescue"; il repository si chiama `canon-powershot-s30-rescue` perché è
così che lo cerca chi ha il problema.

### Avvio dai sorgenti

Serve **Python 3.9+** con Tkinter (Tk 8.6+). Nessun pacchetto di terze parti è
obbligatorio.

```bash
git clone https://github.com/gabrielepetteno/canon-powershot-s30-rescue.git
cd canon-powershot-s30-rescue

./run.sh          # macOS / Linux (oppure doppio clic su "RetroCam Rescue.command")
run.bat           # Windows
```

Gli script di avvio individuano un interprete adatto, verificano che Tkinter si
importi, impostano `PYTHONPATH=src` e aprono l'interfaccia. Si forza con
`RETROCAM_PYTHON=/percorso/di/python`.

Come pacchetto:

```bash
pip install .                 # oppure pipx install .
retrocam                      # interfaccia grafica
retrocam-cli --cli            # senza finestra, sola lettura: rileva ed elenca. Da incollare nelle segnalazioni
pip install ".[image]"        # Pillow: decodifica completa dell'immagine durante la verifica
pip install ".[windows]"      # pywin32: abilita il backend WIA
```

Su macOS serve `brew install gphoto2` per raggiungere un corpo pre-PTP; su Linux
servono `gphoto2` e `python3-tk`. **Non esiste un gphoto2 funzionante per Windows
nativo**: lì la strada è un lettore di schede, oppure WSL2 con `usbipd-win` che
inoltra il dispositivo USB dentro Linux.

### Eseguire i test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

387 test, circa 11 secondi, nessuna rete, nessuna fotocamera, nessun privilegio
di amministratore, niente lasciato in giro. Sei vengono saltati senza Pillow.
Girano anche sotto pytest:

```bash
uv run --group dev python -m pytest -q
```

Verificati su Python 3.9 e 3.14, sia con `unittest` sia con `pytest`. La CI
esegue la suite su 3.9 e 3.13 prima di costruire qualunque artefatto di release
(`.github/workflows/build.yml`, avviato dai tag `v*`). Due tornate di mutation
testing, 86 mutanti, 0 sopravvissuti.

### Come si innestano i backend

Ogni trasporto è una sottoclasse di `CameraBackend`
([`src/retrocam/backends/base.py`](src/retrocam/backends/base.py)) che implementa
`is_available`, `detect`, `list_files`, `download`, `delete`. L'interfaccia
grafica non importa mai un backend direttamente: lo chiede a
[`registry.py`](src/retrocam/registry.py), che tiene un elenco statico ordinato
per affidabilità — mass storage, gphoto2, WIA.

Per aggiungerne uno: estendi `CameraBackend` in un nuovo modulo sotto
`src/retrocam/backends/`, aggiungi un `BackendKind` in
[`model.py`](src/retrocam/model.py), e aggiungi un import statico più una voce in
`ALL_BACKENDS` dentro `registry.py`. L'import deve restare statico: una scoperta
pigra o basata su `importlib` produce silenziosamente un elenco di backend
**vuoto** dentro una build congelata con PyInstaller.

Il contratto è documentato in cima a `base.py`. Le regole portanti: non sollevare
mai un'eccezione grezza (avvolgila in un `CameraError` con un messaggio su cui
una persona non tecnica e spaventata possa agire), non scrivere mai sul
dispositivo al di fuori di `delete()`, riportare le dimensioni esatte in byte da
`list_files()`, e controllare il token di annullamento tra un file e l'altro.
`TransferEngine.delete_verified` in
[`transfer.py`](src/retrocam/transfer.py) è l'unico punto dell'intero programma
che chiama `backend.delete()`.

Le segnalazioni di bug e quelle sull'hardware sono contributi a tutti gli effetti
— vedi [CONTRIBUTING.md](CONTRIBUTING.md). I dettagli sulla costruzione dei
binari sono in [packaging/build.md](packaging/build.md).

### Licenza

MIT — vedi [LICENSE](LICENSE). Usalo, modificalo, incorporalo, vendilo.

Nessuna garanzia. Questo software cancella file dalle schede di memoria quando
glielo chiedi e, per quanto si sforzi parecchio di dimostrare prima che ne esista
una copia buona, i backup restano una tua responsabilità.

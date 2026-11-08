# 4. fázis: a hiányzó szerverfunkciók elkészültek

A 3. fázis visszajelzésében kért szerverfunkciók elkészültek és élesben futnak a `http://192.168.1.120:8090` címen, ugyanazzal az `X-API-Key` kulccsal. A szabályok a korábbiak: titkot ne írj ki, a nyomtatóra parancsot csak a felhasználó jóváhagyásával küldj, a szerverhez ne nyúlj, a flash előtt kérdezz.

**Feladat:** a panel használja ezeket, és vedd ki belőle a megfelelő saját logikát. Ahol a szerver másképp válaszol, mint ahogy itt írom, ne kerüld meg: írd le pontosan.

## 1. Listakép: az első tálca

- A `GET /api/sd/files` minden eleme új mezőt kapott: `"thumb_plate"`, a fájl első tálcájának indexe (pl. `5`).
- A `GET /api/sd/thumb?name=&size=` a `plate` paraméter nélkül is működik: ilyenkor a fájl első tálcájának képét adja.
- **A panelen:** a listaképhez hagyd el a `plate`-et, vagy küldd a `thumb_plate` értékét.

## 2. Leképezés és filament-ellenőrzés: `POST /api/sd/forecast` és `POST /api/cloud/forecast`

Ugyanazok a szabályok, mint a webappban és a panel `computeAmsMap()` / `forecastTrays()` függvényében:
- **Leképezés:** előbb pontos típus és szín, aztán típus és legközelebbi szín; minden AMS-tálca egyszer; ha az AMS-ben nincs egyező, a külső spula.
- **Előrejelzés:** a `now` (van) a címke szerinti súly mínusz a könyvelt fogyás, mínusz a futó nyomtatás eddigi fogyása.

**Kérés:**
- SD-fájlhoz: `POST /api/sd/forecast {"name", "size", "plates": [4, 5, 6], "trays": [-1, 2]}`.
- Felhős újranyomtatáshoz: `POST /api/cloud/forecast {"task_id", "trays"}`.

**A kérés mezői:**
- `plates`: a sor tálcái, az első az, amelyik elsőként indul. Ha elhagyod, a fájl első tálcáját veszi. Többtálcás sornál a szerver slotonként összeadja a grammokat.
- `trays`: nem kötelező. Ez a felhasználó kézi választása filament-slotonként (index = slot − 1): tálca-id, vagy `-1` = automatikus.

**Válasz (mindig 200):**

```json
{"state": "ready",
 "map": [0, 1], "auto": [0, 1],
 "filaments": [{"slot": 1, "type": "PLA", "color": "#FF0000", "grams": 5.6}],
 "rows": [{"tray": 0, "label": "A1", "slots": [1], "type": "PLA", "color": "#FF0000", "spool_id": 7,
           "need": 5.6, "now": 746.0, "after": 740.4, "state": "enough", "text": "Enough"}],
 "ok": true, "missing": false, "warning": ""}
```

**A válasz mezői:**
- `map`: ezt küldd változatlanul a `POST /api/sd/print` vagy a `POST /api/cloud/reprint` `trays` mezőjében.
- `auto`: az automatikus leképezés a kézi választások nélkül.
- `rows[].state`: `enough`, `low`, `short`, `missing`, `no_usage` vagy `no_spool`. A `text` a megjeleníthető angol felirat.
- `rows[].tray` = `-2`: a filamentnek nincs betöltött tálcája. Ilyenkor a `label` = `#slot`, és a `missing` = `true`.
- `ok` = `false`: van `short` vagy `missing` sor. A `warning` ekkor kész szöveg a megerősítéshez, pl. `"A2: need 80 g, have 70 g"`.
- `state` = `loading` vagy `queued`: az SD-fájlt még olvassa az app, pár másodperc múlva kérd újra. `state` = `error`: az `error` mezőben az ok.

**Ezzel kivehető a panelből:**
- `AmsMapping.h` `computeAmsMap()`;
- `FilesPage.cpp` `forecastTrays()` és `shortageText()`;
- az `sdBuildRun()` filament-összevonása.

A panel csak a sorokat és a kézi választást mutassa.

## 3. Élő spulisúly a `/api/state`-ben

- **Mező:** nyomtatás közben az `assignments[].spool` új mezőt kap, `weight_used_live` = könyvelt fogyás + a futó nyomtatás eddigi fogyása.
- **Hiánya:** ha a mező nincs ott (nincs nyomtatás, vagy az a tálca nem fogyott), a `weight_used` az érvényes.
- **A panelen:** a `/api/v1/inventory/assignments` 30 másodperces külön lekérése elhagyható.

## 4. A `last_end` filamentjei: ezek már eddig is megvoltak

A `/api/state` `last_end` objektuma:
- **`filament`:** `[{"slot", "type", "color", "grams", "meters", "where"}]`.
- **`filament_state`:**
  - `loading`: az app még gyűjti az adatokat; legfeljebb pár percig tart;
  - `done`: kész;
  - `none`: nincs adat.

A kész képernyő ezt használja, és ne kérje külön a `/api/v1/archives/` és a filament-requirements útvonalat. Ha a `filament_state` = `loading`, a kép a 2 másodperces állapotlekérésből magától frissül, ezért a kész képernyő saját újrapróbái kivehetők.

**Új mezők:**
- `used_by_tray`: `[{"tray", "label", "grams"}]`, tálcánként az app által nyomtatás közben számolt fogyás. Azonnal megvan.
- `started_at`: unix idő.

## 5. A szünet oka: `/api/state` `pause`

- **Szünetben:** `{"reason", "message", "advice"}`.
  - A `reason` értékei: `runout`, `tangle`, `clog`, `failure`, `first_layer`, `door`, `temperature`, `plate`, `collision`, `printer` (egyéb nyomtatóüzenet) és `user`.
  - A `message` a nyomtató HMS-szövege.
  - Az `advice` a teendő, angolul. A szabályok a panel `pauseAdvice()` kulcsszavai.
- **Nem szünetben:** `null`.

**A panelen:** a `PrintScreens.cpp` `pauseAdvice()` kivehető.

## 6. Monoton események

A `/api/state` két új mezője:
- **`boot_id`:** véletlen azonosító, az app minden indulásakor megváltozik. Ha változik, a `print_starts` / `print_ends` / `print_pauses` számlálók újra nulláról indulnak: vedd fel az új értékeket alapnak, és ne számold eseménynek.
- **`last_start_at`:** a legutóbbi nyomtatásindítás unix ideje. Újraindulás után egy futó nyomtatásnál a naplóból visszaszámolt érték, `0`, ha nincs.

A `last_end.ended_at` már eddig is megvolt. A `countEvents()` így egyszerűsíthető.

## Ami a panelen maradhat

Ezek megjelenítési döntések, nyugodtan maradhatnak a panelen:
- a 35 °C-os „Ready to remove” küszöb;
- `plateClearNeeded()`;
- az Obico-ikon színe;
- a „Lights off” gomb viselkedése;
- a felhőlejárat-figyelmeztetés;
- `preheatTemp()`;
- `nightFinishNote()`, mert a panel saját éjszakai sávját használja.

## A PSRAM-ról

Az induláskori 0 MB-os PSRAM-esés kockázatos. Az előnézeteket ne dekódold egyszerre: sorban, a láthatóakat először, és a lista görgetésekor töltsd a többit. Ha ez nem elég, menj vissza 40 fájlra.

## Visszajelzés

Build, majd a felhasználó jóváhagyásával flash. Végül írd le röviden:
- mit vettél ki;
- mi maradt a panelen és miért;
- ha valami a szerverről nem úgy jön, ahogy itt áll: útvonal, mit kaptál, mit vártál.

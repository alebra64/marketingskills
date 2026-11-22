"""Istruttoria riservata: reclamo GDPR su AdER, senza invio PEC o pubblicazione dati."""
from __future__ import annotations
import os, datetime, threading, json
from functools import wraps
from flask import jsonify, Response, request

CASE_ID='ader-pratica-principale'
JOB_ID='reclamo-garante-ader'
PROC_LOCK=threading.Lock()
BUSY=set()

ROLE_ORDER=[
 ('privacy_bo zza','privacy'),
]

def register(APP, require_auth, safe_post, fetch_practice, dbconn, ask, BASE, NAMES):
    def ensure_table():
        with dbconn() as cn:
            cn.execute("""CREATE TABLE IF NOT EXISTS reclamo_staff (
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'BOZZA',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                stages JSONB NOT NULL DEFAULT '{}'::jsonb,
                history JSONB NOT NULL DEFAULT '[]'::jsonb,
                final TEXT NOT NULL DEFAULT '',
                issues TEXT NOT NULL DEFAULT '',
                approval TEXT NOT NULL DEFAULT ''
            )""")
            cn.execute("ALTER TABLE reclamo_staff ADD COLUMN IF NOT EXISTS history JSONB NOT NULL DEFAULT '[]'::jsonb")

    def get_job():
        with dbconn() as cn:
            r=cn.execute('SELECT status,stages,final,issues,approval,updated_at,history FROM reclamo_staff WHERE id=%s',(JOB_ID,)).fetchone()
        if not r:return None
        return dict(zip(('status','stages','final','issues','approval','updated_at','history'),r))

    def patch(**kwargs):
        fields=', '.join(k+'=%s'+('::jsonb' if k in ('stages','history') else '') for k in kwargs)
        vals=[json.dumps(v,ensure_ascii=False) if k in ('stages','history') else v for k,v in kwargs.items()]
        with dbconn() as cn:
            cn.execute('UPDATE reclamo_staff SET '+fields+',updated_at=now() WHERE id=%s',(*vals,JOB_ID))

    def run_job(force=False, claimed=False):
        with PROC_LOCK:
            if JOB_ID in BUSY and not claimed:return
            BUSY.add(JOB_ID)
        try:
            p=fetch_practice(CASE_ID)
            if not p or p.get('status')!='COMPLETATO':
                print('CONSIGLIO_RECLAMO error=SOURCE_REPORT_NOT_COMPLETE',flush=True)
                return
            ensure_table()
            with dbconn() as cn:
                cn.execute("INSERT INTO reclamo_staff(id,case_id,status) VALUES(%s,%s,'AVVIATO') ON CONFLICT (id) DO NOTHING",(JOB_ID,CASE_ID))
            job=get_job()
            if job['status']=='REVISIONATO' and job.get('final') and not force:
                print('CONSIGLIO_RECLAMO already_done=true',flush=True)
                return
            stages={} if force else dict(job.get('stages') or {})
            patch(status='IN_CORSO',issues='')
            system=BASE.format(today=datetime.date.today().isoformat())
            question=p['quesito']
            report=p.get('final') or ''
            initial=p.get('stages') or {}
            guidance=initial.get('coordinatore',{}).get('indicazioni_uso_rapporto','')
            official=[
              'Procedura Garante: https://www.garanteprivacy.it/diritti/come-agire-per-tutelare-i-tuoi-dati-personali/reclamo',
              'Requisiti: interessato, residenza/luogo lavoro o violazione Italia; titolare del trattamento; fatti e richieste pregresse; norme presumibilmente violate; misure richieste; elenco documenti e firma.',
              'PEC Garante: protocollo@pec.gpdp.it (accetta solo PEC).',
              'Firma digitale o autografa con copia documento riconoscimento; eventuale procura se agisce rappresentante.',
              'Testo GDPR: https://eur-lex.europa.eu/legal-content/IT/TXT/?uri=CELEX:32016R0679',
              'Istruzioni: reclamo art. 77 GDPR e artt. 140-bis-143 Codice Privacy; valutare richiesta provvedimenti ai sensi art. 58 GDPR.',
            ]
            sources=p.get('sources') or []
            source_register='\n'.join(
                '%s | %s | %s | estratto: %s' % (
                   s.get('riferimento',''),s.get('stato',''),s.get('url',''),
                   (s.get('testo') or '')[:2200])
                for s in sources)
            base=(
                'OGGETTO ESCLUSIVO: possibile violazione del principio GDPR di minimizzazione per dati del debitore comunicati da Agenzia Entrate-Riscossione (AdER) al datore di lavoro tramite atti di pignoramento presso terzi. '
                'L interessato NON contesta il diritto di AdER di pignorare lo stipendio. '
                'NON trasformare la controversia in opposizione a pignoramento o impugnazione credito. '
                'La comunicazione di dettagli delle cartelle, enti impositori, vicende di notifica eccedenti le informazioni necessarie per la trattenuta è il cuore del reclamo. '
                'Non asserire come dimostrati contenuti di allegati mai acquisiti o esiti normativi non verificati. '
                'Se mancano gli atti originali di notifica al datore o la risposta PEC di AdER, segnalarli come DOCUMENTI DA CONFERMARE/ALLEGARE. '
                'Non inventare i dati anagrafici (luogo/data nascita, CF, indirizzo, PEC) e lascia campi [DA COMPLETARE]. '
                'Non allegare automaticamente tutte le cartelle o informazioni eccedenti: minimizza anche la documentazione del reclamo. '
                'NON INVIARE NESSUNA PEC. Output in italiano, tono professionale, fermo e rigoroso. '
                'Non attribuire responsabilità personali non dimostrate o affermare violazione già accertata. '
            )
            def runstep(step,role,mission,input_text):
                if stages.get(step,{}).get('stato')=='COMPLETATO' and stages[step].get('output'):
                    print('CONSIGLIO_RECLAMO step='+step+' cached=true',flush=True)
                    return stages[step]['output']
                print('CONSIGLIO_RECLAMO step='+step+' status=START',flush=True)
                used_role=role
                try:
                    output=ask(role,system+' '+base+' '+mission,input_text)
                except Exception as primary_error:
                    # Conserva incarico professionale, ma usa un diverso modello tecnico
                    # se Anthropic non restituisce testo; non inventa esiti del modello originario.
                    if role=='coordinatore':
                        raise
                    used_role='coordinatore'
                    print('CONSIGLIO_RECLAMO step='+step+' primary_failed='+type(primary_error).__name__+' backup_provider=openai',flush=True)
                    output=ask('coordinatore',system+' '+base+' Agisci come specialista '+role+'. '+mission,input_text[:38000])
                stages[step]={'role':role,'provider_effettivo':used_role,'stato':'COMPLETATO','output':output,'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
                patch(stages=stages)
                print('CONSIGLIO_RECLAMO step='+step+' status=COMPLETATO chars='+str(len(output)),flush=True)
                return output
            context=(
              'CASO (fatti dichiarati dall interessato, salvo prova):\n'+question[:21000]+
              '\nRAPPORTO FINALE DEL CONSIGLIO (versione riservata completata):\n'+report[:42000]+
              '\nISTRUZIONI DEL COORDINATORE:\n'+guidance[:9000]+
              '\nINFORMAZIONI FORMALI GARANTE DA SITO ISTITUZIONALE:\n'+'\n'.join(official)+
              '\nREGISTRO DELLE 16 FONTI CON ESTRATTI:\n'+source_register[:30000]
            )
            first=runstep('privacy_prima_bozza','privacy',
              'Redigi il primo testo completo e rigoroso di reclamo ex art.77 GDPR indirizzato al Garante, comprensivo di oggetto PEC, dati reclamante con segnaposto, titolare, cronologia, contestazione specifica per categorie di dati, quadro normativo, difese AdER e loro critica, richieste all Autorità, documenti allegati solo se davvero presenti e mancanti da acquisire. Non allegare il rapporto AI come prova definitiva. Usa i riferimenti esatti delle fonti consultate.',
              context[:104000])
            admin=runstep('verifica_amministrativa','amministrativo',
              'Verifica criticamente l accuratezza di cronologia, notifiche e riferimenti alle comunicazioni AdER nella bozza. Riporta correzioni numerate e quali affermazioni non sono sostenibili senza atti originali. Non trattare la legittimità sostanziale del pignoramento.',
              'QUESITO:\n'+question[:13000]+'\nBOZZA PRIVACY:\n'+first[:23000]+'\nRAPPORTO:\n'+report[:15000])
            tribut=runstep('verifica_riscossione','tributario',
              'Verifica con rigore i limiti di inferenza dal testo art.72-bis DPR 602/1973: quali dati operativi servono al terzo e quali dettagli non sono certamente imposti. Evita di affermare assoluta inutilità o divieti non provati. Restituisci modifiche puntuali alla bozza.',
              'BOZZA:\n'+first[:22000]+'\nRAPPORTO:\n'+report[:13000]+'\nFONTE:\n'+source_register[:19000])
            fonti=runstep('verifica_fonti','fonti',
              'Controlla singolarmente le norme e i riferimenti della bozza contro il registro di testi effettivamente consultati. Evidenzia le citazioni senza prova testuale, i paragrafi inesatti, la differenza art.5(1)(c) e art.5(2), art.6(1)(e), art.25, art.77, art.58, art.72-bis. Controlla che non si affermi la vigenza storica senza prova. Fornisci elenco preciso delle correzioni indispensabili.',
              'BOZZA:\n'+first[:18000]+'\nREGISTRO:\n'+source_register[:33000]+'\nREGOLA UFFICIALE GARANTE:\n'+'\n'.join(official))
            opp=runstep('revisione_avversariale','contraddittorio',
              'Rappresenta la più forte possibile difesa di AdER circa necessità dei dati per evitare errori o identificare crediti, e identifica lacune probatorie e doglianze insufficienti. Proponi correzioni concrete al reclamo per renderlo più robusto, senza nuove accuse.',
              'BOZZA:\n'+first[:20000]+'\nVERIFICA AMMINISTRATIVA:\n'+admin[:9000]+'\nVERIFICA TRIBUTARIA:\n'+tribut[:9000]+'\nVERIFICA FONTI:\n'+fonti[:9000])
            final_privacy=runstep('privacy_bozza_rivista','privacy',
              'Sei il redattore principale. Prepara la VERSIONE RIVISTA E COMPLETA DEL RECLAMO pronta per revisione finale sulla base di tutte le osservazioni. Produci in testa: DESTINATARIO PEC e OGGETTO; poi ATTO DI RECLAMO completo formato trasmissibile, senza osservazioni interne nel corpo. Dopo il reclamo separa una sezione RISERVATA NON DA INVIARE, con documenti da inserire e verifiche obbligatorie prima della spedizione. Includi firme e identificazione come nel modello Garante. Non dichiarare allegato ciò che non è disponibile.',
              'BOZZA PRIMA:\n'+first[:21000]+'\nREVISIONE AMMINISTRATIVA:\n'+admin[:7500]+'\nREVISIONE RISCOSSIONE:\n'+tribut[:7500]+'\nREVISIONE FONTI:\n'+fonti[:9000]+'\nREVISIONE AVVERSARIA:\n'+opp[:11000]+'\nDATI DEL CASO:\n'+question[:10000]+'\nFONTI:\n'+source_register[:23000])
            review=runstep('revisione_finale','revisore',
              'Controlla indipendentemente il testo rivisto come revisore finale. Evidenzia solo problemi materiali ancora aperti: fatti non dimostrati, attribuzioni, fonti, campi mancanti, potenziali errori nel reclamo. Distingui revisioni obbligatorie prima di invio da migliorie e annota se il testo è utilizzabile solo come bozza soggetta ad allegati.',
              'TESTO RIVISTO:\n'+final_privacy[:29000]+'\nFONTI:\n'+source_register[:26000]+'\nRAPPORTO:\n'+report[:9000])
            coordinator=runstep('approvazione_coordinatore','coordinatore',
              'Fornisci decisione finale sul testo nel formato ESITO: APPROVABILE COME BOZZA oppure NON APPROVABILE e MOTIVAZIONE. Devi verificare che non sia presentato come completo e inviabile senza confronto con atti originali. Elenca chi deve fare cosa prima dell invio. Non riscrivere autonomamente il reclamo; non indicare completato per l invio senza documenti e dati firmatario.',
              'TESTO DA APPROVARE:\n'+final_privacy[:26000]+'\nCONTROLLO REVISORE FINALE:\n'+review[:14000]+'\nCONSEGNA UTENTE:\nScrivere reclamo al Garante sulla minimizzazione dei dati AdER; nessun invio automatico.')
            missing='Il reclamo NON e stato inviato. Prima della PEC verificare dati identificativi, atti originali e allegati indicati dal coordinatore.'
            patch(status='REVISIONATO',final=final_privacy,issues=review,approval=coordinator)
            print('CONSIGLIO_RECLAMO FINISHED status=REVISIONATO stages='+str(len(stages))+' final_chars='+str(len(final_privacy)),flush=True)
        except Exception as exc:
            try:patch(status='DA_REVISIONARE',issues=(type(exc).__name__+': '+str(exc))[:320])
            except Exception:pass
            print('CONSIGLIO_RECLAMO FAILURE type='+type(exc).__name__+' detail='+str(exc)[:120],flush=True)
        finally:
            with PROC_LOCK:BUSY.discard(JOB_ID)

    @APP.get('/api/cases/<pid>/reclamo/stato')
    @require_auth
    def reclamo_status(pid):
        if pid!=CASE_ID:return jsonify(error='Non trovato'),404
        job=get_job()
        if not job:return jsonify(status='NON_AVVIATO',stages=[]),200
        return jsonify(status=job['status'],stages=list(job.get('stages') or {}),
            final_ready=bool(job.get('final')),updated_at=job['updated_at'].isoformat(),
            issues=job.get('issues',''),approval=job.get('approval',''))

    @APP.post('/api/cases/<pid>/reclamo/riesegui')
    @require_auth
    @safe_post
    def reclamo_rerun(pid):
        if pid!=CASE_ID:return jsonify(error='Non trovato'),404
        if not request.is_json or not request.json or request.json.get('consent') is not True:
            return jsonify(error='Conferma esplicita dell’invio al Consiglio e dei possibili costi API richiesta'),400
        p=fetch_practice(CASE_ID)
        if not p or p.get('status')!='COMPLETATO':
            return jsonify(error='Il rapporto principale del Consiglio deve essere completato prima di rieseguire il reclamo'),409
        ensure_table()
        with PROC_LOCK:
            if JOB_ID in BUSY:return jsonify(error='Revisione del reclamo già in corso'),409
            BUSY.add(JOB_ID)
        try:
            with dbconn() as cn:
                cn.execute("INSERT INTO reclamo_staff(id,case_id,status) VALUES(%s,%s,'AVVIATO') ON CONFLICT (id) DO NOTHING",(JOB_ID,CASE_ID))
            job=get_job() or {}
            history=list(job.get('history') or [])
            if job.get('final') or job.get('approval'):
                history.append({
                    'status':job.get('status',''),
                    'final':job.get('final',''),
                    'issues':job.get('issues',''),
                    'approval':job.get('approval',''),
                    'updated_at':job.get('updated_at').isoformat() if job.get('updated_at') else ''
                })
            patch(status='AVVIATO',stages={},final='',issues='',approval='',history=history[-90:])
            threading.Thread(target=run_job,args=(True,True),daemon=True).start()
        except Exception as exc:
            with PROC_LOCK:BUSY.discard(JOB_ID)
            return jsonify(error='Impossibile avviare la nuova revisione: '+type(exc).__name__),500
        return jsonify(ok=True,status='AVVIATO',archiviate=len(history)),202

    @APP.get('/api/cases/<pid>/reclamo/download')
    @require_auth
    def reclamo_download(pid):
        if pid!=CASE_ID:return jsonify(error='Non trovato'),404
        job=get_job()
        if not job or not job['final']:return jsonify(error='Reclamo non ancora disponibile'),404
        body=(
            '# Reclamo AdER al Garante Privacy — BOZZA DA VERIFICARE E FIRMARE\n\n'+
            '> Documento non inviato. Identita, allegati e originali devono essere controllati prima della PEC.\n\n'+
            job['final']+
            '\n\n---\n## Note finali riservate del Revisore\n\n'+job.get('issues','')+
            '\n\n---\n## Decisione e condizioni del Coordinatore\n\n'+job.get('approval','')
        )
        return Response(body,mimetype='text/markdown; charset=utf-8',
          headers={'Content-Disposition':'attachment; filename="Bozza_Reclamo_Garante_AdER.md"',
                   'Cache-Control':'private, no-store'})

    @APP.get('/api/cases/<pid>/reclamo/testo')
    @require_auth
    def reclamo_text(pid):
        if pid!=CASE_ID:return jsonify(error='Non trovato'),404
        job=get_job()
        if not job or not job['final']:return jsonify(error='Reclamo non ancora disponibile'),404
        return jsonify(status=job['status'],testo=job['final'],verifica=job['issues'],coordinatore=job['approval'])

    @APP.get('/api/cases/<pid>/reclamo/atto')
    @require_auth
    def reclamo_atto(pid):
        if pid!=CASE_ID:return jsonify(error='Non trovato'),404
        job=get_job()
        if not job or not job['final']:return jsonify(error='Bozza non ancora disponibile'),404
        return Response(job['final'],mimetype='text/markdown; charset=utf-8',
          headers={'Content-Disposition':'attachment; filename="Atto_Reclamo_Garante_AdER_BOZZA.md"',
                   'Cache-Control':'private, no-store'})

    ensure_table()
    _job=get_job()
    if _job and _job.get('status')=='REVISIONATO':
        _approval=(_job.get('approval') or '').upper()[:600]
        _decision=('NON_APPROVABILE' if 'NON APPROVABILE' in _approval or 'NON APPROVABILE' in _approval
                   else 'APPROVABILE_COME_BOZZA' if 'APPROVABILE COME BOZZA' in _approval
                   else 'VERDETTO_DA_LEGGERE')
        print('CONSIGLIO_RECLAMO_STATUS saved=true stages='+str(len(_job.get('stages') or {}))+
          ' chars='+str(len(_job.get('final') or ''))+' approval='+_decision,flush=True)
    if _job and _job.get('status')=='REVISIONATO' and os.getenv('RECLAMO_AUDIT_APPROVAL','0')=='1':
        # Solo spiegazione del Coordinatore; eventuali dati identificativi mascherati nei log.
        import re
        _txt=(_job.get('approval') or '')
        _txt=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', _txt)
        _txt=re.sub(r'\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b','[CF]',_txt)
        for _i in range(0,len(_txt),380):
            print('CONSIGLIO_RECLAMO_APPROVAL_TEXT '+str(_i//380+1)+' '+_txt[_i:_i+380].replace('\n','⏎'),flush=True)
    if os.getenv('RECLAMO_DRAFT_AT_BOOT','0')=='1':
        threading.Thread(target=run_job,daemon=True).start()

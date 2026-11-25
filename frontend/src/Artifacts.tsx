import {useEffect, useState} from 'react';
import {parseArtifacts, parseReport, type ArtifactListing, type RetainedReport} from './contracts';

export function Artifacts({itemId,runId}:{itemId:string;runId:string}) {
  const [listing,setListing]=useState<ArtifactListing|null>(null);
  const [report,setReport]=useState<RetainedReport|null>(null);
  const [reportOpen,setReportOpen]=useState(true);
  const [galleryOpen,setGalleryOpen]=useState(true);
  const [revision,setRevision]=useState(0);
  const [loading,setLoading]=useState(false);
  const [reportLoading,setReportLoading]=useState(false);
  const [error,setError]=useState('');
  const [reportError,setReportError]=useState('');
  const [failedImages,setFailedImages]=useState<Record<string,boolean>>({});
  const base=`/api/v1/work/${encodeURIComponent(itemId)}`;
  const query=`run_id=${encodeURIComponent(runId)}&revision=${revision}`;
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);setError('');
    async function load() {
      try {
        const response=await fetch(`${base}/artifacts?${query}`,{signal:controller.signal});
        if(!response.ok)throw new Error(response.status===409?'This task changed attempts. Waiting for refreshed task details.':'Could not load retained artifacts.');
        const next=parseArtifacts(await response.json());
        if(next.item_id!==itemId || next.run_id!==runId)throw new Error('Artifacts no longer match the selected run. Refresh task details.');
        if(!controller.signal.aborted)setListing(next);
      } catch(e) {if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Could not load retained artifacts.');}
      finally {if(!controller.signal.aborted)setLoading(false);}
    }
    void load();return()=>controller.abort();
  },[base,query,itemId,runId]);
  useEffect(()=>{
    if(!reportOpen)return;
    const controller=new AbortController();setReportLoading(true);setReportError('');
    async function load() {
      try {
        const response=await fetch(`${base}/report?${query}`,{signal:controller.signal});
        if(!response.ok)throw new Error(response.status===409?'This task changed attempts. Refresh task details.':'The retained report is unavailable. Try refreshing retained files.');
        const next=parseReport(await response.json());
        if(next.item_id!==itemId || next.run_id!==runId)throw new Error('Report no longer matches the selected run.');
        if(!controller.signal.aborted)setReport(next);
      } catch(e) {if(!controller.signal.aborted)setReportError(e instanceof Error?e.message:'Report refresh failed.');}
      finally {if(!controller.signal.aborted)setReportLoading(false);}
    }
    void load();return()=>controller.abort();
  },[base,query,itemId,runId,reportOpen]);
  return <section className="artifacts" aria-label="Retained run artifacts">
    <div className="artifact-heading"><h4>Report and screenshots</h4><button disabled={loading} onClick={()=>{setRevision(n=>n+1);setFailedImages({});}}>Refresh retained files</button></div>
    {error&&<p className="error" role="alert">{error} {listing&&'Showing the last successful listing.'}</p>}
    {loading&&!listing&&<p role="status">Looking for retained files.</p>}
    {listing&&<>
      {listing.report_status==='available'?<>
        <button className="report-toggle" aria-expanded={reportOpen} aria-controls="retained-report" onClick={()=>setReportOpen(open=>!open)}>{reportOpen?'Hide report':'Read retained report'}</button>
        {reportOpen&&<div id="retained-report">
          {reportLoading&&<p role="status">Refreshing report.</p>}
          {reportError&&<p className="error" role="alert">{reportError} {report&&'Showing the last successful report.'}</p>}
          {report&&<><p className="muted">Retained report, shown as text. Links and embedded content stay inactive.</p><pre className="retained-report" tabIndex={0}>{report.text}</pre>{report.truncated&&<p className="muted">This report exceeds the preview limit. Showing the first 256 KiB; the retained file is unchanged.</p>}</>}
        </div>}
      </>:<p className="muted">{listing.report_status==='missing'?'No report has been retained yet.':'The retained report is unavailable in this preview.'}</p>}
      <details open={galleryOpen} onToggle={e=>setGalleryOpen(e.currentTarget.open)}><summary>Screenshots ({listing.screenshots.length})</summary>
        {!listing.screenshots.length&&<p className="muted">No readable screenshots were found for this run.</p>}
        {listing.screenshots_unavailable&&<p className="muted">Some screenshot files are unavailable or could not be safely opened.</p>}
        {listing.screenshots_truncated&&<p className="muted">This gallery shows a bounded selection of up to 24 files. More retained evidence remains in the existing dashboard.</p>}
        {galleryOpen&&<div className="screenshot-grid">{listing.screenshots.map(shot=>{
          const url=`${base}/screenshots/${encodeURIComponent(shot.name)}?${query}`;
          return <figure key={shot.name}>{failedImages[shot.name]?<p className="muted">This screenshot is no longer available. Refresh retained files to try again.</p>:<a href={url} target="_blank" rel="noreferrer"><img key={url} src={url} alt={`Retained screenshot ${shot.name}`} loading="lazy" onError={()=>setFailedImages(previous=>({...previous,[shot.name]:true}))}/></a>}<figcaption>{shot.name.replace(/\.png$/,'').replaceAll('-',' ')}</figcaption></figure>;
        })}</div>}
      </details>
    </>}
  </section>;
}

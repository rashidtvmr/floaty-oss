'use strict';
const cv = window.CaptureVerification;
const $ = id => document.getElementById(id);
let stream = null, baseline = null, after = null, seq = 0;
const video = $('preview'), canvas = $('frame');
function status(verdict, message) { $('result').dataset.verdict = verdict; $('result').textContent = message; }
function isLive(){ return !!(stream && stream.getVideoTracks()[0].readyState === 'live'); }
function settings(){ return stream ? stream.getVideoTracks()[0].getSettings() : {}; }
function controls(){
 $('share').disabled = isLive();
 $('stop').disabled = !isLive();
 $('baseline').disabled = !isLive();
 $('after').disabled = !isLive() || !baseline || !baseline.marker.detected;
 $('save-baseline').disabled = !baseline;
 $('save-after').disabled = !after;
 $('save-report').disabled = !baseline && !after;
}
function stop(){
 if(stream) stream.getTracks().forEach(t=>t.stop());
 stream=null; video.srcObject=null;
 $('stream-info').textContent='Not sharing.';controls();
}
async function share(){
 if(!navigator.mediaDevices?.getDisplayMedia){status('inconclusive','Use localhost or HTTPS.');return;}
 try{
  const selected=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'monitor'},audio:false});
  stop();stream=selected;seq++;baseline=null;after=null;
  video.srcObject=selected;
  selected.getVideoTracks()[0].onended=()=>{if(stream===selected) stop();};
  try{await video.play();}catch(e){}
  $('stream-info').textContent='Surface: '+(settings().displaySurface||'unknown');
  status('inconclusive','Capture baseline with marker visible.');controls();
 }catch(e){status('inconclusive','Share not started: '+e.message);}
}
async function capture(which){
 if(!isLive()||!video.videoWidth){status('inconclusive','Captured frame not ready.');return;}
 canvas.width=video.videoWidth;canvas.height=video.videoHeight;
 const ctx=canvas.getContext('2d',{willReadFrequently:true});
 ctx.drawImage(video,0,0);
 try{
  const marker=cv.detectMarker(ctx.getImageData(0,0,canvas.width,canvas.height));
  const png=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
  if(!png) throw new Error('PNG encode failed');
  const item={marker,png,seq,width:canvas.width,height:canvas.height,
   surface:settings().displaySurface||'unknown',at:new Date().toISOString()};
  if(which==='baseline'){baseline=item;after=null;$('after-info').textContent='After: not captured.';}
  else after=item;
  $(which+'-info').textContent=which+': '+(marker.detected?'MARKER DETECTED':'marker missing');
  if(which==='baseline') status('inconclusive',marker.detected?'Baseline valid: enable exclusion and capture again.':'Baseline missing: check selected monitor.');
  else evaluate();controls();
 }catch(e){status('inconclusive','Frame analysis failed: '+e.message);}
}
function evaluate(){
 const result=cv.evaluateComparison({baseline:baseline?.marker,protectedFrame:after?.marker,
  sameStream:!!(baseline&&after&&baseline.seq===after.seq),
  monitorSelected:after?.surface==='monitor',locallyVisible:$('locally-visible').checked});
 status(result.verdict,result.message);
}
function download(blob,name){
 const url=URL.createObjectURL(blob),a=document.createElement('a');
 a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1500);
}
function report(){
 const summary=i=>i&&{time:i.at,found:i.marker.detected,width:i.width,height:i.height,
  surface:i.surface,stream:i.seq};
 const data={schema:1,baseline:summary(baseline),after:summary(after),
  locallyVisible:$('locally-visible').checked,verdict:$('result').dataset.verdict};
 download(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),'socket_stream_report.json');
}
$('share').addEventListener('click',share);
$('stop').addEventListener('click',()=>{stop();status('inconclusive','Sharing stopped.');});
$('baseline').addEventListener('click',()=>capture('baseline'));
$('after').addEventListener('click',()=>capture('after'));
$('save-baseline').addEventListener('click',()=>baseline&&download(baseline.png,'socket_stream_baseline.png'));
$('save-after').addEventListener('click',()=>after&&download(after.png,'socket_stream_after.png'));
$('save-report').addEventListener('click',report);
$('locally-visible').addEventListener('change',()=>{if(after)evaluate();});
controls();

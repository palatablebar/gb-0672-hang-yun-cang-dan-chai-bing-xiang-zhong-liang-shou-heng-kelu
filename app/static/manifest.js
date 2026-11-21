const ui={busy:false};
const output=document.querySelector('#message');
const runId=document.body.dataset.runId;
document.querySelector('#print').addEventListener('click',()=>window.print());

function goTo(params){
  const url=new URL(location.href);
  Object.entries(params).forEach(([key,value])=>{if(value===null||value===undefined||value==='')url.searchParams.delete(key);else url.searchParams.set(key,value);});
  location.href=url;
}

document.querySelector('#execute').addEventListener('click',async()=>{
  if(ui.busy)return;
  ui.busy=true;output.textContent='核验中…';
  try{
    const dataset=document.querySelector('#sample').value;
    const evidenceDataset=document.querySelector('#evidence-sample').value;
    const sameBase=dataset===document.body.dataset.source;
    const sameEvidence=evidenceDataset===(document.body.dataset.evidenceSource||'');
    let result;
    if(sameBase&&sameEvidence){
      const response=await fetch('/api/runs/'+runId+'/rerun',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      result=await response.json();if(!response.ok)throw Error(result.detail);
    }else{
      const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset,evidence_dataset:evidenceDataset||null})});
      result=await response.json();if(!response.ok)throw Error(result.detail);
    }
    goTo({dataset,evidence_dataset:evidenceDataset||null,run_id:result.manifest.run_id});
  }catch(e){output.textContent=e.message;}finally{ui.busy=false;}
});

document.querySelector('#query').addEventListener('input',event=>document.querySelectorAll('.data-panel tbody tr').forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(event.target.value.toLowerCase())));
document.querySelector('#sample').value=new URL(location.href).searchParams.get('dataset')||document.querySelector('#sample').value;

const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}

/* 箱门口径切换：同一指令重试不新增版本；校验不过时服务端 422，页面保持原版本。 */
async function switchBasis(switches){
  if(ui.busy)return;
  ui.busy=true;output.textContent='切换核对中…';
  try{
    const response=await fetch('/api/runs/'+runId+'/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:Number(document.body.dataset.revision||0)||undefined,handover:{switches}})});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail);
    location.reload();
  }catch(e){output.textContent='未切换：'+e.message+('；仍保留原申报与旧封签');}finally{ui.busy=false;}
}
document.querySelectorAll('[data-switch-basis]').forEach(button=>button.addEventListener('click',()=>{
  const container=button.dataset.container;
  const basis=button.dataset.switchBasis;
  const delta=button.dataset.delta||'0';
  let note='';
  if(basis==='weighing'){
    const answer=window.prompt(container+' 改按称重口径：请写明差异来源（如复磅、皮重更正）。当前差额 '+delta+' kg。','');
    if(answer===null)return;
    note=answer.trim();
  }
  switchBasis([{container,basis,note}]);
}));

const versionList=document.querySelector('#version-list');
async function loadVersions(){
  if(!versionList)return;
  try{
    const response=await fetch('/api/runs/'+runId+'/versions');
    const rows=await response.json();
    if(!response.ok)throw Error(rows.detail);
    if(!rows.length){versionList.innerHTML='<span class="muted">暂无登记版本。</span>';return;}
    const current=Number(document.body.dataset.version||0);
    versionList.innerHTML=rows.map(row=>{
      const switches=row.instruction&&row.instruction.switches?row.instruction.switches:null;
      const label=switches&&switches.length?switches.map(item=>item.container+'→'+item.basis).join(' · '):'默认口径';
      return '<div class="vrow"><span class="'+(row.version_no===current?'current':'')+'">v'+row.version_no+'</span><span>'+label+'</span><button type="button" data-restore="'+row.version_no+'">恢复</button></div>';
    }).join('');
    versionList.querySelectorAll('[data-restore]').forEach(button=>button.addEventListener('click',()=>restore(button.dataset.restore)));
  }catch(e){versionList.innerHTML='<span class="muted">版本读取失败：'+e.message+'</span>';}
}
async function restore(versionNo){
  const response=await fetch('/api/runs/'+runId+'/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version_no:Number(versionNo)})});
  const data=await response.json();
  if(!response.ok){if(output)output.textContent=data.detail;return;}
  location.reload();
}
if(versionList){loadVersions();document.querySelector('#restore-latest').addEventListener('click',()=>restore(1));}
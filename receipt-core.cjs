(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ReceiptCore=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const BASES=['reported','estimated','unknown','incomplete'];
  const CATEGORIES=['coding','research','background'];
  const text=(value,max=150)=>typeof value==='string'&&value.trim()&&value.length<=max?value.trim():null;
  const number=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=1e8;
  function cleanProof(proof,label){
    if(proof===undefined)return undefined;
    if(!proof||typeof proof!=='object'||Array.isArray(proof))throw new Error(`${label}: comparison must be an object.`);
    const out={};
    for(const field of ['reference_task_id','workload_sha256','reference_workload_sha256','evaluator']){
      if(proof[field]!==undefined){if(!text(proof[field],150))throw new Error(`${label}: invalid ${field}.`);out[field]=proof[field].trim();}
    }
    if(proof.checks!==undefined){
      if(!Array.isArray(proof.checks)||proof.checks.length>100)throw new Error(`${label}: supply at most 100 quality checks.`);
      const seen=new Set();
      out.checks=proof.checks.map(check=>{
        if(!check||!text(check.name,80)||typeof check.candidate_passed!=='boolean'||typeof check.reference_passed!=='boolean')throw new Error(`${label}: every quality check needs a name and two boolean results.`);
        if(seen.has(check.name.trim()))throw new Error(`${label}: quality-check names must be unique.`);seen.add(check.name.trim());
        return {name:check.name.trim(),candidate_passed:check.candidate_passed,reference_passed:check.reference_passed};
      });
    }
    return out;
  }
  function validate(value){
    if(!value||typeof value!=='object'||Array.isArray(value)||value.currency!=='USD')throw new Error('Use a receipt JSON object with currency set to "USD".');
    if(!Array.isArray(value.tasks)||value.tasks.length<1||value.tasks.length>5000)throw new Error('Include between 1 and 5,000 tasks.');
    const ids=new Set();
    const tasks=value.tasks.map((row,i)=>{
      const label=`Task ${i+1}`;
      if(!row||typeof row!=='object'||Array.isArray(row)||!text(row.name))throw new Error(`${label} needs a name of 1–150 characters.`);
      if(!CATEGORIES.includes(row.category))throw new Error(`${label}: category must be coding, research, or background.`);
      if(row.calls!==null&&(!Number.isSafeInteger(row.calls)||row.calls<0||row.calls>1e9))throw new Error(`${label}: calls must be a nonnegative whole number, or null when unknown.`);
      const id=row.id===undefined?`task-${i+1}`:text(row.id,150);if(!id||ids.has(id))throw new Error(`${label}: task IDs must be nonempty and unique.`);ids.add(id);
      const out={id,name:row.name.trim(),category:row.category,calls:row.calls};
      for(const key of ['cost','reference_cost','alternative_cost']){
        if(key!=='cost'&&row[key]===undefined)continue;
        if(row[key]!==null&&!number(row[key]))throw new Error(`${label}: ${key} must be a finite nonnegative USD amount, or null when unknown.`);
        out[key]=row[key];
      }
      for(const key of ['cost_basis','reference_cost_basis']){out[key]=row[key]===undefined?'unknown':row[key];if(!BASES.includes(out[key]))throw new Error(`${label}: invalid ${key}.`);}
      for(const key of ['model','reference_model']){if(row[key]!==undefined){if(!text(row[key],150))throw new Error(`${label}: invalid ${key}.`);out[key]=row[key].trim();}}
      const proof=cleanProof(row.comparison,label);if(proof)out.comparison=proof;
      return out;
    });
    const billing=value.billing===undefined?'unknown':value.billing;if(!['metered','subscription','unknown'].includes(billing))throw new Error('billing must be metered, subscription, or unknown.');
    const source=value.source===undefined?'normalized':value.source;if(!['openclaw','hermes','normalized'].includes(source))throw new Error('Unsupported receipt source.');
    return {schema_version:2,currency:'USD',billing,source,is_sample:value.is_sample===true,coverage:value.coverage==='partial'?'partial':'complete',warnings:Array.isArray(value.warnings)?value.warnings.filter(x=>text(x,240)).slice(0,10):[],tasks};
  }
  function evidence(row,receipt){
    const proof=row.comparison||{};const reasons=[];
    if(receipt.billing!=='metered')reasons.push('Metered billing not confirmed');
    if(row.cost===null||row.reference_cost===null||row.reference_cost===undefined)reasons.push('Both costs are required');
    if(row.cost_basis!=='reported'||row.reference_cost_basis!=='reported')reasons.push('Both costs must be reported, not estimated');
    if(!row.model||!row.reference_model||row.model===row.reference_model||['unknown','mixed'].includes(row.model.toLowerCase())||['unknown','mixed'].includes(row.reference_model.toLowerCase()))reasons.push('Two different known model routes are required');
    if(!proof.reference_task_id||receipt.tasks.some(task=>task.id===proof.reference_task_id))reasons.push('A distinct reference run is required');
    if(proof.reference_task_id&&receipt.tasks.filter(task=>task.comparison?.reference_task_id===proof.reference_task_id).length>1)reasons.push('A reference run cannot be counted twice');
    if(!/^[a-f0-9]{64}$/.test(proof.workload_sha256||'')||proof.workload_sha256!==proof.reference_workload_sha256)reasons.push('Workload fingerprints must match');
    if(!proof.evaluator||!proof.checks?.length||!proof.checks.every(x=>x.candidate_passed===true&&x.reference_passed===true))reasons.push('Both runs must pass the supplied quality checks');
    return {passed:reasons.length===0,reasons,checks:proof.checks?.length||0};
  }
  function summarize(receipt,category='all'){
    const rows=receipt.tasks.filter(row=>category==='all'||row.category===category);
    const hasCosts=rows.length>0&&rows.every(row=>number(row.cost));
    const hasReference=hasCosts&&rows.every(row=>number(row.reference_cost));
    const total=rows.reduce((n,row)=>n+(row.cost??0),0);
    const reference=hasReference?rows.reduce((n,row)=>n+row.reference_cost,0):null;
    const recorded=hasReference?reference-total:null;
    const checked=rows.map(row=>({row,...evidence(row,receipt)}));
    const allVerified=rows.length>0&&receipt.coverage==='complete'&&checked.every(row=>row.passed);
    const full=hasCosts&&receipt.coverage==='complete'&&rows.every(row=>row.cost_basis!=='incomplete');
    const hasAlternative=full&&rows.every(row=>number(row.alternative_cost));
    const opportunity=hasAlternative?rows.reduce((n,row)=>n+row.cost-row.alternative_cost,0):null;
    const verified=allVerified?recorded:null;
    let share=null;
    if(hasReference&&reference>0&&recorded>0&&full){share={percent:Math.round(recorded/reference*100),kind:allVerified?'verified':'unverified',label:allVerified?'RECEIPT CHECKS PASSED':'UNVERIFIED COMPARISON',description:'SPEND REDUCTION VS. REFERENCE'};}
    else if(hasAlternative&&total>0&&opportunity>0){share={percent:Math.round(opportunity/total*100),kind:'estimate',label:'ESTIMATE',description:'POTENTIAL SPEND REDUCTION'};}
    return {rows,total,reference,recorded,verified,opportunity,hasCosts,hasReference,hasAlternative,allVerified,full,checked,share,calls:rows.reduce((n,row)=>n+(row.calls??0),0),callsKnown:rows.every(row=>row.calls!==null),estimated:rows.some(row=>row.cost_basis==='estimated'),checks:checked.reduce((n,row)=>n+row.checks,0)};
  }
  const sample=validate({currency:'USD',billing:'metered',source:'hermes',is_sample:true,tasks:[
    {id:'candidate-code',name:'Coding & debugging',category:'coding',calls:312,cost:15.20,reference_cost:24.10,alternative_cost:13.50,cost_basis:'reported',reference_cost_basis:'reported',model:'efficient-model-b',reference_model:'premium-model-a',comparison:{reference_task_id:'reference-code',workload_sha256:'a'.repeat(64),reference_workload_sha256:'a'.repeat(64),evaluator:'test-suite-v1',checks:[{name:'Regression suite',candidate_passed:true,reference_passed:true}]}},
    {id:'candidate-research',name:'Research & synthesis',category:'research',calls:186,cost:7.30,reference_cost:12.40,alternative_cost:6.50,cost_basis:'reported',reference_cost_basis:'reported',model:'efficient-model-b',reference_model:'premium-model-a',comparison:{reference_task_id:'reference-research',workload_sha256:'b'.repeat(64),reference_workload_sha256:'b'.repeat(64),evaluator:'research-rubric-v1',checks:[{name:'Citation rubric',candidate_passed:true,reference_passed:true}]}},
    {id:'candidate-background',name:'Background tasks',category:'background',calls:786,cost:1.88,reference_cost:6.30,alternative_cost:1.50,cost_basis:'reported',reference_cost_basis:'reported',model:'efficient-model-b',reference_model:'premium-model-a',comparison:{reference_task_id:'reference-background',workload_sha256:'c'.repeat(64),reference_workload_sha256:'c'.repeat(64),evaluator:'triage-rules-v1',checks:[{name:'Classification check',candidate_passed:true,reference_passed:true}]}}
  ]});
  return {validate,evidence,summarize,sample};
});

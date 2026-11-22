import {useState} from 'react';
import {label,money,num,record,rows,str,useGet,collection,type Session} from './api';
import {Feedback,Heading,Loading,Pager,type Navigate} from './ui';
export function SettingsPage({session,teams=false}:{session:Session|null;teams?:boolean}){
  const settings=session?.settings;
  return <><Heading title={teams?'Model teams':'Settings'} description={teams?'Inspect the named lineups available to this workspace.':'Inspect the active policy and model assignments without exposing credentials.'}/>
    {!settings?.configured&&<section className="panel"><p>Load configuration when starting this interface to inspect models and policy.</p></section>}
    {settings?.configured&&<><section className="panel"><h2>Current team: {settings.team||'Configured roles'}</h2><div className="table-scroll"><table><thead><tr><th>Role</th><th>Model</th><th>Fallbacks</th></tr></thead><tbody>{settings.roles.map(role=><tr key={role.role}><td>{label(role.role)}</td><td>{role.model}</td><td>{role.fallbacks.join(', ')||'None'}</td></tr>)}</tbody></table></div></section>
      {!teams&&<section className="panel"><h2>Execution defaults</h2><dl>{Object.entries(settings.limits).map(([key,value])=><div key={key}><dt>{label(key)}</dt><dd>{key==='budget_usd'?money(value):key==='max_runtime_s'?`${value/3600} hours`:String(value)}</dd></div>)}</dl><p className="muted">Individual task/project allowances still apply. Configuration editing, secrets and experimental speed options remain in the existing dashboard.</p></section>}
      {teams&&<div className="card-grid">{settings.teams.map(team=><section className="panel" key={team.name}><h2>{team.name}</h2><p>{team.description}</p><dl>{Object.entries(team.models).map(([role,model])=><div key={role}><dt>{label(role)}</dt><dd>{model}</dd></div>)}</dl></section>)}</div>}
    </>}
  </>;
}

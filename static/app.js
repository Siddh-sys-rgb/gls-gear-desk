const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
let state = { user: null, csrf: null, selected: null };
const today = new Date();
const localISO = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

async function api(path, options={}) {
  const headers = {...(options.body ? {'Content-Type':'application/json'} : {}), ...(options.headers||{})};
  if (state.csrf && options.method && options.method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(path, {...options, headers});
  let data = {}; try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function flash(text, error=false) { const el=$('#flash'); el.textContent=text; el.className=`message ${error?'error':''}`; clearTimeout(flash.timer); flash.timer=setTimeout(()=>el.classList.add('hidden'),4500); }
function showApp(user) {
  state.user=user; $('#loginView').classList.add('hidden'); $('#appView').classList.remove('hidden'); $('#userArea').classList.remove('hidden');
  $('#userChip').textContent=`${user.name} · ${user.role}`; $('#greeting').textContent=`Welcome, ${user.name.split(' ')[0]}`;
  $('#adminTab').classList.toggle('hidden', user.role!=='admin'); loadInventory();
}
async function restore() { try { const d=await api('/api/me'); state.csrf=d.csrfToken; showApp(d.user); } catch (_) {} }

$('#bookingDate').value=localISO(new Date(today.getFullYear(),today.getMonth(),today.getDate()+1)); $('#bookingDate').min=localISO(today); $('#bookingDate').max=localISO(new Date(today.getFullYear(),today.getMonth(),today.getDate()+120));
$$('.credential').forEach(b=>b.onclick=()=>{ $('#email').value=b.dataset.email; $('#password').value=b.dataset.password; });
$('#loginForm').onsubmit=async e=>{e.preventDefault(); $('#loginError').classList.add('hidden'); try{const d=await api('/api/login',{method:'POST',body:JSON.stringify({email:$('#email').value,password:$('#password').value})});state.csrf=d.csrfToken;showApp(d.user)}catch(err){$('#loginError').textContent=err.message;$('#loginError').classList.remove('hidden')}};
$('#logoutBtn').onclick=async()=>{try{await api('/api/logout',{method:'POST',body:'{}'})}finally{location.reload()}};
$('#bookingDate').onchange=loadInventory;

async function loadInventory(){
  const box=$('#inventory'); box.innerHTML='<div class="empty">Loading availability…</div>';
  try{const d=await api(`/api/items?date=${encodeURIComponent($('#bookingDate').value)}`); let free=0; box.innerHTML=d.items.map(item=>{free+=Object.values(item.availability).filter(Boolean).length;return `<article class="equipment-card"><div><span class="category">${esc(item.category)}</span><h3>${esc(item.name)}</h3><p class="muted tiny">${esc(item.description)}</p></div><div class="slot-grid">${d.slots.map(slot=>`<button class="slot" ${item.availability[slot]?'':'disabled'} data-id="${item.id}" data-name="${esc(item.name)}" data-slot="${slot}">${slot[0].toUpperCase()+slot.slice(1)}<small>${slot==='morning'?'8:00–12:00':'1:00–5:00'}</small></button>`).join('')}</div></article>`}).join('');$('#availableCount').textContent=free;$$('.slot:not(:disabled)').forEach(b=>b.onclick=()=>openBooking(b));}catch(err){box.innerHTML=`<div class="empty">${esc(err.message)}</div>`}
}
function openBooking(button){state.selected={itemId:Number(button.dataset.id),itemName:button.dataset.name,slot:button.dataset.slot,date:$('#bookingDate').value};$('#dialogTitle').textContent=`Reserve ${state.selected.itemName}`;$('#dialogSummary').textContent=`${prettyDate(state.selected.date)} · ${state.selected.slot} · ${state.selected.slot==='morning'?'8:00–12:00':'1:00–5:00'}`;$('#purpose').value='';$('#dialogError').classList.add('hidden');$('#bookingDialog').showModal();}
$('.dialog-close').onclick=()=>$('#bookingDialog').close();
$('#bookingForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/bookings',{method:'POST',body:JSON.stringify({...state.selected,purpose:$('#purpose').value})});$('#bookingDialog').close();flash('Reservation confirmed. See it in My bookings.');loadInventory();}catch(err){$('#dialogError').textContent=err.message;$('#dialogError').classList.remove('hidden')}};

$$('.tab').forEach(tab=>tab.onclick=()=>{ $$('.tab').forEach(t=>t.classList.remove('active'));tab.classList.add('active');$$('.panel-view').forEach(p=>p.classList.add('hidden'));$(`#${tab.dataset.view}Panel`).classList.remove('hidden');if(tab.dataset.view==='mine')loadMine();if(tab.dataset.view==='admin')loadAdmin(); });
function prettyDate(iso){return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric',year:'numeric'})}
function dateTile(iso){const d=new Date(`${iso}T12:00:00`);return `<div class="date-tile"><small>${d.toLocaleDateString(undefined,{month:'short'})}</small><b>${d.getDate()}</b></div>`}
function bookingRow(b, admin=false){let action='';if(!admin&&b.status==='reserved')action=`<button class="small-btn cancel" data-id="${b.id}">Cancel</button>`;if(admin&&b.status==='reserved')action=`<button class="primary action" data-id="${b.id}" data-action="checkout">Check out</button>`;if(admin&&b.status==='checked_out')action=`<button class="primary action" data-id="${b.id}" data-action="return">Mark returned</button>`;return `<article class="booking-row">${dateTile(b.booking_date)}<div><h3>${esc(b.item_name)} <span class="status ${b.status}">${esc(b.status.replace('_',' '))}</span></h3><div class="booking-meta">${esc(b.slot)} · ${esc(b.purpose)}${admin?` · ${esc(b.user_name)} (${esc(b.user_email)})`:''}</div></div><div class="row-actions">${action}</div></article>`}
async function loadMine(){const box=$('#myBookings');box.innerHTML='<div class="empty">Loading…</div>';try{const d=await api('/api/bookings');box.innerHTML=d.bookings.length?`<div class="booking-list">${d.bookings.map(b=>bookingRow(b)).join('')}</div>`:'<div class="empty">No bookings yet. Choose an available slot to get started.</div>';$$('.cancel').forEach(b=>b.onclick=async()=>{if(!confirm('Cancel this reservation?'))return;try{await api(`/api/bookings/${b.dataset.id}/cancel`,{method:'POST',body:'{}'});flash('Reservation cancelled. The slot is available again.');loadMine();loadInventory();}catch(err){flash(err.message,true)}})}catch(err){box.innerHTML=`<div class="empty">${esc(err.message)}</div>`}}
async function loadAdmin(){const box=$('#adminBookings');box.innerHTML='<div class="empty">Loading…</div>';try{const d=await api('/api/admin/bookings');box.innerHTML=d.bookings.length?`<div class="booking-list">${d.bookings.map(b=>bookingRow(b,true)).join('')}</div>`:'<div class="empty">No reservations in the queue.</div>';$$('.action').forEach(b=>b.onclick=async()=>{try{await api(`/api/admin/${b.dataset.id}/${b.dataset.action}`,{method:'POST',body:'{}'});flash(b.dataset.action==='checkout'?'Equipment checked out.':'Return recorded.');loadAdmin();loadInventory();}catch(err){flash(err.message,true)}})}catch(err){box.innerHTML=`<div class="empty">${esc(err.message)}</div>`}}
restore();

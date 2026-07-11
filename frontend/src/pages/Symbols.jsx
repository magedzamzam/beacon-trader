import { useEffect, useState } from "react";
import { Plus, Trash2, Pencil } from "lucide-react";
import { Table, Card, Th, Td, Empty } from "../components/ui";
import { Modal, Field, Input, NumberInput, Select, Button, ErrorNote } from "../components/form";
import { api } from "../lib/api";

export default function Symbols() {
  const [symbols, setSymbols] = useState([]);
  const [brokers, setBrokers] = useState([]);
  const [err, setErr] = useState(null);
  const [editing, setEditing] = useState(null);
  const load = async () => {
    try { setSymbols(await api.symbols()); setBrokers(await api.brokers()); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  return (
    <div className="space-y-6">
      <ErrorNote>{err}</ErrorNote>
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Symbol maps</div>
          <Button onClick={() => setEditing("new")}><Plus className="w-4 h-4 inline -mt-0.5" /> Add mapping</Button>
        </div>
        <div className="px-4 py-2 text-[11px] text-warn border-b border-edge">
          value_per_point is money per 1.0 price move per 1.0 size — calibrate it per broker or sizing will be wrong.
        </div>
        {!symbols.length ? <Empty>No symbol maps. Add XAUUSD → your broker’s gold epic.</Empty> : (
          <Table>
            <thead><tr className="border-b border-edge">
              <Th>Internal</Th><Th>Broker epic</Th><Th right>value/point</Th><Th right>min lot</Th>
              <Th right>lot step</Th><Th right>min dist</Th><Th right></Th>
            </tr></thead>
            <tbody>
              {symbols.map(s => (
                <tr key={s.id} className="border-b border-edge/60">
                  <Td mono>{s.internal_symbol}</Td><Td mono>{s.broker_epic}</Td>
                  <Td right mono>{s.value_per_point}</Td><Td right mono>{s.min_lot}</Td>
                  <Td right mono>{s.lot_step}</Td><Td right mono>{s.min_stop_distance ?? "—"}</Td>
                  <Td right>
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" onClick={() => setEditing(s)}><Pencil className="w-4 h-4" /></Button>
                      <Button variant="danger" onClick={async () => { await api.deleteSymbol(s.id); load(); }}><Trash2 className="w-4 h-4" /></Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {editing && <SymbolModal sym={editing === "new" ? null : editing} brokers={brokers}
        onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </div>
  );
}

function SymbolModal({ sym, brokers, onClose, onSaved }) {
  const s = sym || {};
  const [brokerId, setBrokerId] = useState(s.broker_id || (brokers[0]?.id ?? ""));
  const [internal, setInternal] = useState(s.internal_symbol || "XAUUSD");
  const [epic, setEpic] = useState(s.broker_epic || "GOLD");
  const [vpp, setVpp] = useState(s.value_per_point ?? 1);
  const [minLot, setMinLot] = useState(s.min_lot ?? 0.01);
  const [lotStep, setLotStep] = useState(s.lot_step ?? 0.01);
  const [minDist, setMinDist] = useState(s.min_stop_distance ?? "");
  const [err, setErr] = useState(null);

  const save = async () => {
    const body = { broker_epic: epic, value_per_point: +vpp, min_lot: +minLot,
      lot_step: +lotStep, min_stop_distance: minDist === "" ? null : +minDist };
    try {
      if (sym) await api.updateSymbol(sym.id, body);
      else await api.createSymbol({ broker_id: +brokerId, internal_symbol: internal, ...body });
      onSaved();
    } catch (e) { setErr(e.message); }
  };
  return (
    <Modal title={sym ? "Edit mapping" : "Add mapping"} onClose={onClose}>
      <ErrorNote>{err}</ErrorNote>
      {!sym && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <Field label="Broker">
            <Select value={brokerId} onChange={e => setBrokerId(e.target.value)}>
              {brokers.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
            </Select>
          </Field>
          <Field label="Internal symbol"><Input mono value={internal} onChange={e => setInternal(e.target.value)} /></Field>
        </div>
      )}
      <Field label="Broker epic" hint="Capital.com market epic, e.g. GOLD"><Input mono value={epic} onChange={e => setEpic(e.target.value)} /></Field>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="value / point"><NumberInput value={vpp} onChange={e => setVpp(e.target.value)} /></Field>
        <Field label="min stop distance"><NumberInput value={minDist} onChange={e => setMinDist(e.target.value)} placeholder="optional" /></Field>
        <Field label="min lot"><NumberInput value={minLot} onChange={e => setMinLot(e.target.value)} /></Field>
        <Field label="lot step"><NumberInput value={lotStep} onChange={e => setLotStep(e.target.value)} /></Field>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>{sym ? "Save" : "Add"}</Button>
      </div>
    </Modal>
  );
}

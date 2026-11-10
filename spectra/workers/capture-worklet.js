/* AudioWorklet: acquisition only; the graph's output is always zero (no monitoring).
 * Works with variable render quantum lengths. Messages are transferred, not copied.
 */
class SpectraCapture extends AudioWorkletProcessor {
  constructor(options){
    super();
    this.block=options.processorOptions?.blockFrames || 1024;
    this.buffer=new Float32Array(this.block);
    this.fill=0;this.selected='auto';this.chosen=0;
    this.channelPower=[0,0];this.report=0;
    this.port.onmessage=(event)=>{
      const m=event.data;
      if(m?.type==='channel')this.selected=m.value;
    };
  }
  process(inputs,outputs){
    // A live, muted output keeps the graph scheduled without feedback.
    for(const channel of (outputs[0] || []))channel.fill(0);
    const channels=inputs[0] || [];
    const frames=channels[0]?.length || 0;
    if(!frames)return true;
    if(this.selected==='auto'){
      for(let c=0;c<Math.min(2,channels.length);c++){
        let p=0;
        for(let i=0;i<frames;i++)p+=channels[c][i]**2;
        this.channelPower[c]=0.95*this.channelPower[c]+0.05*(p/frames);
      }
      // Hysteresis prevents rapid channel switching during short silences.
      const alternate=this.chosen===0?1:0;
      if(alternate<channels.length && this.channelPower[alternate] > 3*this.channelPower[this.chosen]+1e-9)
        this.chosen=alternate;
    } else {
      this.chosen=Math.min(Math.max(Number(this.selected)-1,0),channels.length-1);
    }
    const inChannel=channels[this.chosen] || channels[0];
    for(let i=0;i<frames;i++){
      this.buffer[this.fill++]=inChannel[i];
      if(this.fill===this.block){
        const transfer=this.buffer;
        this.port.postMessage({type:'audio',pcm:transfer.buffer,channels:channels.length,selected:this.chosen+1},[transfer.buffer]);
        this.buffer=new Float32Array(this.block);
        this.fill=0;
      }
    }
    return true;
  }
}
registerProcessor('spectra-capture',SpectraCapture);

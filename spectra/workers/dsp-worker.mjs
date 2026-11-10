import {StreamingSTFT, VoiceAnalyzer} from '../src/dsp-core.mjs';
let stft=null,voice=null,fs=48000,epoch=0;
let lastVoiceSample=0, voiceHop=3840,clippingSinceLastAnalysis=false;
let demoTimer=null,demoFrame=0,phase=0,seed=12;
function stopDemo(){if(demoTimer!==null){clearInterval(demoTimer);demoTimer=null;}}
function configure({sampleRate=48000,fftSize=2048,hopSize=512,epoch:requestedEpoch=0}){
  stopDemo();fs=sampleRate;epoch=requestedEpoch;
  stft=new StreamingSTFT(fs,fftSize,hopSize);
  voice=new VoiceAnalyzer(fs);
  lastVoiceSample=0;voiceHop=Math.round(fs*0.085);clippingSinceLastAnalysis=false;
  postMessage({type:'configured',epoch,fs,fftSize,hopSize,pitchObservationMs:voice.observationMs});
}
function consume(pcm,ack=false){
  if(!stft)return;
  const columns=[],transfers=[];
  stft.push(pcm,(frame)=>{
    columns.push({pcmEnd:frame.endSample,db:frame.db.buffer});
    transfers.push(frame.db.buffer);
  });
  for(let i=0;i<pcm.length;i++){if(Math.abs(pcm[i])>=0.999){clippingSinceLastAnalysis=true;break;}}
  voice.push(pcm);
  let metrics=null;
  if(voice.written-lastVoiceSample>=voiceHop){
    metrics=voice.analyze();metrics.clipped=clippingSinceLastAnalysis;
    clippingSinceLastAnalysis=false;lastVoiceSample=voice.written;
  }
  if(columns.length || metrics || ack)postMessage({type:'data',epoch,columns,metrics,audioAck:ack},transfers);
}
function demoStep(){
  const size=1024,x=new Float32Array(size);
  const notes=[220.0,261.625565,329.627557,392.0];
  for(let i=0;i<size;i++){
    const t=(demoFrame+i)/fs,part=Math.floor(t/3.5)%notes.length;
    const f=notes[part],local=t%3.5;
    const env=Math.min(1,local/0.065)*Math.min(1,(3.5-local)/0.065);
    const vibrato=1+0.002*Math.sin(2*Math.PI*4.5*t);
    phase+=2*Math.PI*f*vibrato/fs;
    if(phase>=2*Math.PI)phase-=2*Math.PI;
    seed=(Math.imul(1664525,seed)+1013904223)>>>0;
    const noise=(seed/0xffffffff-0.5)*0.002;
    x[i]=env*(0.28*Math.sin(phase)+0.12*Math.sin(2*phase)+0.05*Math.sin(3*phase)+noise);
  }
  demoFrame+=size;consume(x);
}
onmessage=(event)=>{
  try{
    const m=event.data;
    switch(m.type){
      case 'configure':configure(m);break;
      case 'audio':if(stft && m.epoch===epoch)consume(new Float32Array(m.pcm),true);break;
      case 'gap':if(m.epoch===epoch){stft.reset();voice.reset();lastVoiceSample=0;clippingSinceLastAnalysis=false;postMessage({type:'gap-ack',epoch});}break;
      case 'start-demo':
        stopDemo();demoFrame=0;phase=0;seed=12;
        if(!stft)configure({sampleRate:48000});
        // Audio in real time; timers may throttle when page is backgrounded.
        demoTimer=setInterval(demoStep,1024/fs*1000);
        break;
      case 'stop-demo':stopDemo();break;
      case 'clear':if(stft)stft.reset();if(voice)voice.reset();lastVoiceSample=0;clippingSinceLastAnalysis=false;break;
      case 'shutdown':stopDemo();close();break;
    }
  }catch(e){postMessage({type:'error',detail:String(e?.message||e)});}
};

/**
 * Spectra Web DSP — portable JavaScript translation of the Spectra Instrument
 * v0.3 STFT/YIN core. No browser dependencies; testable in Node.
 *
 * STFT: causal N-point periodic Hann, 1-sided peak dBFS (bin-centered tone).
 * YIN: causal FIR-decimated CMNDF, threshold 0.16, parabola refinement.
 * The harmonic ratio H2..H8/H1 is NOT vocal THD or a classifier of drive.
 * SPDX-License-Identifier: MIT
 */
const PI = Math.PI;
const EPS = 1e-12;
export const MIN_DB = -120;
export const NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
export function midiToNote(midi) {
  if (!Number.isInteger(midi) || midi < 0 || midi > 127) return '—';
  return NOTE_NAMES[midi % 12] + String(Math.floor(midi / 12) - 1);
}
export function noteToFreq(midi) { return 440 * Math.pow(2, (midi - 69) / 12); }
export function freqToNote(freq) {
  if (!(freq > 0) || !Number.isFinite(freq)) return null;
  const m = 69 + 12 * Math.log2(freq / 440), midi = Math.round(m);
  return { midi, name: midiToNote(midi), cents: 100 * (m - midi) };
}
export function clamp(x,a,b) { return Math.max(a, Math.min(b,x)); }

export class FFT {
  constructor(n) {
    if (!Number.isInteger(n) || n < 256 || n > 16384 || (n & (n-1))) throw Error('FFT size must be a power of two, 256..16384');
    this.n = n;
    this.real = new Float64Array(n);
    this.imag = new Float64Array(n);
    this.cos = new Float64Array(n/2);
    this.sin = new Float64Array(n/2);
    for (let i=0;i<n/2;i++) {
      const a = -2 * PI * i / n;
      this.cos[i] = Math.cos(a); this.sin[i] = Math.sin(a);
    }
  }
  transform() {
    const n=this.n, r=this.real, im=this.imag;
    for(let i=1,j=0; i<n; i++) {
      let bit=n>>1;
      for(;j & bit; bit>>=1) j^=bit;
      j^=bit;
      if(i<j){let t=r[i];r[i]=r[j];r[j]=t;t=im[i];im[i]=im[j];im[j]=t;}
    }
    for(let len=2;len<=n;len*=2) {
      const half=len/2, twStep=n/len;
      for(let base=0;base<n;base+=len) {
        for(let j=0;j<half;j++) {
          const a=base+j,b=a+half, idx=j*twStep;
          const tr=r[b]*this.cos[idx]-im[b]*this.sin[idx];
          const ti=r[b]*this.sin[idx]+im[b]*this.cos[idx];
          r[b]=r[a]-tr;im[b]=im[a]-ti;r[a]+=tr;im[a]+=ti;
        }
      }
    }
  }
}

export class StreamingSTFT {
  constructor(sampleRate, fftSize=2048, hopSize=512) {
    if (!Number.isFinite(sampleRate) || sampleRate<8000 || sampleRate>384000) throw Error('Unsupported sample rate');
    if(!Number.isInteger(hopSize) || hopSize<=0 || hopSize>fftSize) throw Error('Invalid hop size');
    this.fs=sampleRate;this.n=fftSize;this.hop=hopSize;
    this.fft=new FFT(fftSize);
    this.ring=new Float32Array(fftSize);
    this.hann=new Float64Array(fftSize);
    let sum=0;
    for(let i=0;i<fftSize;i++){this.hann[i]=0.5-0.5*Math.cos(2*PI*i/fftSize);sum+=this.hann[i];}
    this.windowSum=sum;
    this.reset();
  }
  reset(){this.ring.fill(0);this.w=0;this.filled=0;this.since=0;this.written=0;}
  push(pcm,onColumn) {
    const n=this.n, ring=this.ring;
    for(let i=0;i<pcm.length;i++) {
      const s=pcm[i];ring[this.w]=Number.isFinite(s)?s:0;this.w=(this.w+1)%n;
      this.written++;
      if(this.filled<n){this.filled++;if(this.filled===n){onColumn(this.emit());this.since=0;}}
      else if(++this.since>=this.hop){onColumn(this.emit());this.since=0;}
    }
  }
  emit(){
    const n=this.n,r=this.fft.real,im=this.fft.imag;
    for(let i=0;i<n;i++){r[i]=this.ring[(this.w+i)%n]*this.hann[i];im[i]=0;}
    this.fft.transform();
    const db=new Float32Array(n/2+1);
    for(let k=0;k<db.length;k++) {
      const scale=(k===0||k===n/2)?1:2;
      const amp=scale*Math.hypot(r[k],im[k])/this.windowSum;
      db[k]=Math.max(MIN_DB,20*Math.log10(Math.max(amp,1e-7)));
    }
    return { db, endSample:this.written };
  }
}

function firForDecimator(d) {
  if(d===1) return Float32Array.of(1);
  const size=12*d+1, fc=0.43/d, taps=new Float32Array(size);
  let sum=0;
  for(let i=0;i<size;i++){
    const t=i-(size-1)/2;
    const sinc=t===0?2*fc:Math.sin(2*PI*fc*t)/(PI*t);
    const w=0.54-0.46*Math.cos(2*PI*i/(size-1));
    taps[i]=sinc*w;sum+=taps[i];
  }
  for(let i=0;i<size;i++) taps[i]/=sum;
  return taps;
}

export class VoiceAnalyzer {
  constructor(sampleRate,minHz=65,maxHz=1050) {
    if (!(sampleRate>=8000 && sampleRate<=384000)) throw Error('Unsupported fs');
    this.fs=sampleRate;
    this.d=Math.max(1,Math.ceil(sampleRate/12000));
    this.dsFs=sampleRate/this.d;
    this.minHz=minHz;this.maxHz=maxHz;
    this.minLag=Math.floor(this.dsFs/maxHz);
    this.maxLag=Math.ceil(this.dsFs/minHz);
    this.W=1024;
    if(maxHz>=this.dsFs/3 || this.minLag<2 || this.maxLag>=this.W) throw Error('Unsupported pitch range');
    this.fir=firForDecimator(this.d);
    this.firRing=new Float32Array(this.fir.length);
    this.ring=new Float32Array(this.W+this.maxLag);
    this.workspace=new Float64Array(this.ring.length);
    this.cmnd=new Float64Array(this.maxLag+1);
    this.hann=new Float64Array(this.W);
    for(let i=0;i<this.W;i++)this.hann[i]=0.5-0.5*Math.cos(2*PI*i/this.W);
    this.reset();
  }
  get observationMs(){return this.ring.length/this.dsFs*1000;}
  reset(){this.firRing.fill(0);this.ring.fill(0);this.firPos=0;this.phase=0;this.pos=0;this.filled=0;this.written=0;this.clipped=false;}
  push(pcm){
    this.clipped=false;
    const n=this.fir.length;
    for(let i=0;i<pcm.length;i++){
      const val=Number.isFinite(pcm[i])?pcm[i]:0;
      if(Math.abs(val)>=0.999)this.clipped=true;
      this.firRing[this.firPos]=val;
      this.firPos=(this.firPos+1)%n;
      if(++this.phase===this.d){
        this.phase=0;
        let out=0;
        for(let j=0;j<n;j++)out+=this.fir[j]*this.firRing[(this.firPos+j)%n];
        this.ring[this.pos]=out;
        this.pos=(this.pos+1)%this.ring.length;
        this.filled=Math.min(this.filled+1,this.ring.length);
      }
    }
    this.written+=pcm.length;
  }
  analyze(){
    const result={voiced:false,clipped:this.clipped,f0:0,confidence:0,midi:-1,note:'—',cents:0,ratio:0,ratioDb:0,rmsDb:-120,endSample:this.written};
    if(this.filled<this.ring.length)return result;
    const v=this.workspace,len=v.length,W=this.W;
    let mean=0;
    for(let i=0;i<len;i++){v[i]=this.ring[(this.pos+i)%len];mean+=v[i];}
    mean/=len;
    let power=0;
    for(let i=0;i<len;i++){
      v[i]-=mean;
      if(i>=len-W)power+=v[i]*v[i];
    }
    const rms=Math.sqrt(power/W);
    result.rmsDb=20*Math.log10(Math.max(1e-7,rms));
    if(result.rmsDb<-52)return result;
    let accum=0;
    this.cmnd[0]=1;
    for(let lag=1;lag<=this.maxLag;lag++) {
      let sum=0;
      for(let i=0;i<W;i++){const q=v[i]-v[i+lag];sum+=q*q;}
      accum+=sum;
      this.cmnd[lag]=sum*lag/Math.max(accum,1e-20);
    }
    let best=0;
    for(let lag=this.minLag;lag<=this.maxLag;lag++) {
      if(this.cmnd[lag]<0.16){
        while(lag<this.maxLag&&this.cmnd[lag+1]<this.cmnd[lag])lag++;
        best=lag;break;
      }
    }
    if(!best)return result;
    let refined=best;
    if(best>1&&best<this.maxLag) {
      const a=this.cmnd[best-1],b=this.cmnd[best],c=this.cmnd[best+1],denom=a-2*b+c;
      if(Math.abs(denom)>1e-8)refined+=clamp(0.5*(a-c)/denom,-0.5,0.5);
    }
    const f0=this.dsFs/refined,confidence=clamp(1-this.cmnd[best],0,1);
    if(f0<this.minHz||f0>this.maxHz||confidence<0.8)return result;
    const note=freqToNote(f0);
    result.voiced=true;result.f0=f0;result.confidence=confidence;
    result.midi=note.midi;result.note=note.name;result.cents=note.cents;
    let first=0,others=0;
    const start=len-W;
    for(let h=1;h<=8;h++) {
      const freq=f0*h;
      if(freq>=0.45*this.dsFs)break;
      const angle=-2*PI*freq/this.dsFs;
      const cs=Math.cos(angle),ss=Math.sin(angle);
      let real=0,imag=0,pr=1,pi=0;
      for(let i=0;i<W;i++) {
        const x=v[start+i]*this.hann[i];
        real+=x*pr;imag+=x*pi;
        const next=pr*cs-pi*ss;
        pi=pr*ss+pi*cs;pr=next;
      }
      const p=real*real+imag*imag;
      if(h===1)first=p;else others+=p;
    }
    if(first>EPS){result.ratio=Math.sqrt(others/first);result.ratioDb=20*Math.log10(Math.max(result.ratio,1e-7));}
    return result;
  }
}

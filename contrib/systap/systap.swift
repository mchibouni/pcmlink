// systap: the Mac's system audio on stdout as raw interleaved 32-bit float,
// minus the processes named with --exclude-pid. Excluding the process that
// plays a stream back on this Mac keeps that stream out of the capture, which
// is what stops a feedback loop when the capture is sent to the same place.
//
// Uses a Core Audio process tap (macOS 14.2+), so no virtual sound card is
// needed. macOS asks once for "System Audio Recording" permission, for the app
// that runs this (the terminal, or Hammerspoon); without it the audio is silent.
//
// usage: systap [--exclude-pid PID]...        stream audio to stdout
//        systap --only-pid PID...             only these processes instead
//        systap --format                      print "RATE CHANNELS" and exit
//
// Build: swiftc -O systap.swift -o systap
import AudioToolbox
import CoreAudio
import Foundation

func fail(_ message: String, _ status: OSStatus = noErr) -> Never {
    let detail = status == noErr ? "" : " (OSStatus \(status))"
    FileHandle.standardError.write("systap: \(message)\(detail)\n".data(using: .utf8)!)
    exit(1)
}

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector,
                               mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

// A process has an audio object only once it has used Core Audio.
func processObject(_ pid: pid_t) -> AudioObjectID? {
    var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var pidValue = pid
    var object = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                                            UInt32(MemoryLayout<pid_t>.size), &pidValue,
                                            &size, &object)
    return status == noErr && object != kAudioObjectUnknown ? object : nil
}

var excluded: [AudioObjectID] = []
var only: [AudioObjectID] = []
var formatOnly = false
var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let arg = args.removeFirst()
    if arg == "--format" {
        formatOnly = true
    } else if arg == "--exclude-pid", !args.isEmpty, let pid = pid_t(args.removeFirst()) {
        // Refuse rather than carry on: capturing a stream that should have been
        // left out is exactly the feedback loop this option exists to prevent.
        guard let object = processObject(pid) else {
            fail("process \(pid) has no audio to exclude (not running, or not playing yet)")
        }
        excluded.append(object)
    } else if arg == "--only-pid", !args.isEmpty, let pid = pid_t(args.removeFirst()) {
        guard let object = processObject(pid) else { fail("process \(pid) has no audio") }
        only.append(object)
    } else {
        fail("usage: systap [--exclude-pid PID]... | systap --only-pid PID... | systap --format")
    }
}

let tap = only.isEmpty ? CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
                       : CATapDescription(stereoMixdownOfProcesses: only)
tap.uuid = UUID()
tap.name = "systap"
tap.isPrivate = true
tap.muteBehavior = .unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(tap, &tapID)
guard status == noErr else { fail("could not create the process tap", status) }

var format = AudioStreamBasicDescription()
var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
var formatAddr = address(kAudioTapPropertyFormat)
status = AudioObjectGetPropertyData(tapID, &formatAddr, 0, nil, &formatSize, &format)
guard status == noErr else { fail("could not read the tap format", status) }
let channels = Int(format.mChannelsPerFrame)
guard format.mFormatID == kAudioFormatLinearPCM,
      format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
      format.mBitsPerChannel == 32 else {
    fail("unexpected tap format: \(format)")
}
if formatOnly {
    print("\(Int(format.mSampleRate)) \(channels)")
    exit(0)
}

// The tap is read through a private aggregate device that holds only the tap.
let aggregate: [String: Any] = [
    kAudioAggregateDeviceNameKey: "systap",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapUIDKey: tap.uuid.uuidString,
        kAudioSubTapDriftCompensationKey: true,
    ]],
]
var deviceID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &deviceID)
guard status == noErr else { fail("could not create the aggregate device", status) }

signal(SIGPIPE, SIG_IGN)
let nonInterleaved = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
let scratch = UnsafeMutablePointer<Float>.allocate(capacity: 1 << 16)

// Writes block when the reader falls behind, which only delays this callback;
// a closed pipe means the reader is gone, so stop.
func put(_ bytes: UnsafeRawPointer, _ count: Int) {
    var done = 0
    while done < count {
        let n = write(1, bytes + done, count - done)
        if n <= 0 { exit(0) }
        done += n
    }
}

var procID: AudioDeviceIOProcID?
status = AudioDeviceCreateIOProcIDWithBlock(&procID, deviceID, nil) { _, input, _, _, _ in
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
    if !nonInterleaved {
        for buffer in buffers where buffer.mData != nil {
            put(buffer.mData!, Int(buffer.mDataByteSize))
        }
        return
    }
    // One buffer per channel: interleave them.
    guard buffers.count >= channels, let first = buffers.first, first.mData != nil else { return }
    let frames = min(Int(first.mDataByteSize) / 4, (1 << 16) / channels)
    for c in 0..<channels {
        guard let data = buffers[c].mData else { return }
        let src = data.assumingMemoryBound(to: Float.self)
        for f in 0..<frames { scratch[f * channels + c] = src[f] }
    }
    put(scratch, frames * channels * 4)
}
guard status == noErr, let procID else { fail("could not create the IO callback", status) }

status = AudioDeviceStart(deviceID, procID)
guard status == noErr else { fail("could not start capturing", status) }

// Tear down on SIGINT or SIGTERM; the private tap and device also go away
// with the process if it is killed outright.
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        AudioDeviceStop(deviceID, procID)
        AudioDeviceDestroyIOProcID(deviceID, procID)
        AudioHardwareDestroyAggregateDevice(deviceID)
        AudioHardwareDestroyProcessTap(tapID)
        exit(0)
    }
    source.resume()
    _ = Unmanaged.passRetained(source as AnyObject)
}
dispatchMain()

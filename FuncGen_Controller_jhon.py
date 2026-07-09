import pyvisa

rm = pyvisa.ResourceManager()
inst = rm.open_resource('USB0::0x0957::0x5707::MY59001023::0::INSTR')

inst.timeout = 5000
print(inst.query('*IDN?'))  # confirm connection

# Set sine wave, 1kHz, 2Vpp, 0V offset
inst.write('SOUR1:APPL:SIN 1000,4')

# Turn on the output
inst.write('OUTP1 ON')

# Check for errors
print(inst.query('SYST:ERR?'))

inst.close()